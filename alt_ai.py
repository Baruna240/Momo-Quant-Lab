"""Model AI untuk simulator futures altcoin: belajar dari candle 1 jam semua koin yang dipantau.

Yang dipelajari: dari kondisi satu candle (momentum, RSI, jarak ke EMA, volatilitas, volume, kondisi BTC),
apakah harga 12 jam ke depan cenderung NAIK, TURUN, atau DATAR (diukur terhadap volatilitas koin itu sendiri).
Model: gradient boosting (scikit-learn), dilatih ulang setiap 24 jam dengan data terbaru.
Semua angka kualitas dihitung di data yang TIDAK ikut dipelajari (out-of-sample), supaya tidak menipu diri sendiri.
"""
import math
import pickle
import time
from contextlib import contextmanager

import numpy as np

from config import BASE_DIR

MODEL_PATH = BASE_DIR / "models" / "alt_ai.pkl"
HORIZON = 12            # candle 1 jam ke depan yang ditebak
THRESH_ATR = 2.0        # NAIK/TURUN kalau bergerak lebih dari 2×ATR, selain itu DATAR
EDGE_Q = 0.90           # hanya bertindak pada 10% prediksi paling yakin
# arah (LONG/SHORT) hanya boleh dipakai kalau di data uji terbukti: cukup banyak sinyal, lebih sering benar,
# dan rata-ratanya lebih besar dari biaya
MIN_SIGNALS, MIN_HIT, MIN_RET = 200, 0.5, 0.2
WARMUP = 200
FEATURES = ["ret 1j", "ret 4j", "ret 12j", "ret 24j", "ret 72j", "RSI", "jarak EMA20", "jarak EMA50", "jarak EMA200",
            "EMA50 vs EMA200", "volatilitas", "volume relatif", "posisi di range 48j", "jam (sin)", "jam (cos)",
            "BTC ret 4j", "BTC ret 24j", "BTC vs EMA200"]


def _arr(xs):
    return np.array([np.nan if x is None else x for x in xs], dtype=float)


def _shift(a, k):
    out = np.full_like(a, np.nan)
    if k > 0:
        out[k:] = a[:-k]
    else:
        out[:k] = a[-k:]
    return out


def _roll(a, n, fn):
    out = np.full_like(a, np.nan)
    if len(a) >= n:
        out[n - 1:] = fn(np.lib.stride_tricks.sliding_window_view(a, n), axis=1)
    return out


def _btc_cols(btc):
    cols = getattr(btc, "_ai_btc", None)
    if cols is None:
        lc, e200 = np.log(np.asarray(btc.c)), _arr(btc.e200)
        cols = np.column_stack([lc - _shift(lc, 4), lc - _shift(lc, 24), np.asarray(btc.c) / e200 - 1])
        btc._ai_btc = cols
    return cols


def features(d):
    """Matriks fitur (satu baris per candle). Disimpan di objek Bars supaya tidak dihitung ulang."""
    x = getattr(d, "_ai_x", None)
    if x is not None:
        return x
    c, h, l, v = (np.asarray(a, dtype=float) for a in (d.c, d.h, d.l, d.v))
    at, e20, e50, e200 = _arr(d.atr), _arr(d.e20), _arr(d.e50), _arr(d.e200)
    lc = np.log(c)
    hi48, lo48 = _roll(h, 48, np.max), _roll(l, 48, np.min)
    hour = (np.asarray(d.t) // 3600) % 24
    with np.errstate(divide="ignore", invalid="ignore"):
        cols = [lc - _shift(lc, k) for k in (1, 4, 12, 24, 72)]
        cols += [_arr(d.rsi) / 100, (c - e20) / at, (c - e50) / at, (c - e200) / at, e50 / e200 - 1, at / c,
                 v / _shift(_roll(v, 20, np.mean), 1), (c - lo48) / (hi48 - lo48),
                 np.sin(2 * math.pi * hour / 24), np.cos(2 * math.pi * hour / 24)]
    btc = getattr(d, "btc", None)
    if btc is not None:
        bc, pos = _btc_cols(btc), {t: k for k, t in enumerate(btc.t)}
        rows = np.array([pos.get(t, -1) for t in d.t])
        b = bc[np.clip(rows, 0, None)]
        b[rows < 0] = np.nan
        cols += [b[:, 0], b[:, 1], b[:, 2]]
    else:
        cols += [np.full(len(c), np.nan)] * 3
    x = np.column_stack(cols)
    x[~np.isfinite(x)] = np.nan   # model gradient boosting bisa menangani nilai kosong
    d._ai_x = x
    return x


def labels(d):
    """Kelas tiap candle: 0 = TURUN, 1 = DATAR, 2 = NAIK (melihat HORIZON candle ke depan)."""
    c, at = np.asarray(d.c, dtype=float), _arr(d.atr)
    fwd = _shift(c, -HORIZON) / c - 1
    thr = THRESH_ATR * at / c
    y = np.where(fwd > thr, 2, np.where(fwd < -thr, 0, 1)).astype(float)
    y[np.isnan(fwd) | np.isnan(thr)] = np.nan
    return y, fwd


def dataset(data, t_end=None):
    """Gabungkan semua koin. Hanya baris yang hasilnya sudah diketahui sebelum t_end."""
    xs, ys, fs, ts = [], [], [], []
    for d in data.values():
        x, (y, fwd) = features(d), labels(d)
        t = np.asarray(d.t)
        t_known = _shift(t, -HORIZON)
        ok = ~np.isnan(y)
        ok[:WARMUP] = False
        if t_end is not None:
            ok &= t_known < t_end
        xs.append(x[ok]); ys.append(y[ok]); fs.append(fwd[ok]); ts.append(t[ok])
    if not xs:
        return None
    x, y, f, t = (np.concatenate(a) for a in (xs, ys, fs, ts))
    order = np.argsort(t, kind="stable")
    return x[order], y[order].astype(int), f[order], t[order]


def _new_model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                          min_samples_leaf=200, l2_regularization=1.0, random_state=7)


def _edge(model, x):
    p = model.predict_proba(x)
    cls = list(model.classes_)
    up = p[:, cls.index(2)] if 2 in cls else 0
    dn = p[:, cls.index(0)] if 0 in cls else 0
    return up - dn


def proven_sides(meta):
    """Arah yang lolos ujian: 1 = LONG, -1 = SHORT."""
    return {side for side, k in ((1, "long"), (-1, "short"))
            if (meta.get(k + "_n") or 0) >= MIN_SIGNALS and (meta.get(k + "_hit") or 0) > MIN_HIT
            and (meta.get(k + "_ret") or 0) > MIN_RET}


def _signal_stats(edge, fwd, thr):
    out = {}
    for name, mask, sign in (("long", edge >= thr, 1), ("short", edge <= -thr, -1)):
        r = sign * fwd[mask]
        out[name + "_n"] = int(mask.sum())
        out[name + "_hit"] = float((r > 0).mean()) if len(r) else None
        out[name + "_ret"] = float(r.mean() * 100) if len(r) else None
    return out


class AiModel:
    def __init__(self):
        self.model, self.edge, self.meta = None, None, {}

    def fit(self, data, t_end=None):
        """Latih: 80% data terlama untuk belajar, 20% terbaru untuk menilai. Lalu latih ulang di semuanya."""
        ds = dataset(data, t_end)
        if ds is None or len(ds[1]) < 5000:
            raise ValueError("data latih terlalu sedikit")
        x, y, fwd, t = ds
        cut = int(len(y) * 0.8)
        m = _new_model().fit(x[:cut], y[:cut])
        edge = _edge(m, x[cut:])
        thr = float(np.quantile(np.abs(edge), EDGE_Q))
        pred = m.predict(x[cut:])
        meta = {"trained_at": time.time(), "samples": int(len(y)), "val_samples": int(len(y) - cut),
                "coins": len(data), "days": float((t[-1] - t[0]) / 86400), "val_days": float((t[-1] - t[cut]) / 86400),
                "acc": float((pred == y[cut:]).mean()),
                "base_acc": float(np.bincount(y[cut:], minlength=3).max() / (len(y) - cut)),
                "dist": [float(v) for v in np.bincount(y, minlength=3) / len(y)],
                "edge": thr, "horizon": HORIZON, **_signal_stats(edge, fwd[cut:], thr)}
        meta["sides"] = sorted(proven_sides(meta))
        self.model, self.edge, self.meta = _new_model().fit(x, y), thr, meta
        return meta

    def edges(self, d):
        cache = getattr(d, "_ai_edge", None)
        if cache is None or cache[0] is not self.model:
            cache = (self.model, _edge(self.model, features(d)))
            d._ai_edge = cache
        return cache[1]

    def signal(self, d, i):
        """1 = long, -1 = short, 0 = tidak ada sinyal (atau arahnya belum terbukti di data uji)."""
        if self.model is None:
            return 0
        e = self.edges(d)[i]
        sig = 1 if e >= self.edge else -1 if e <= -self.edge else 0
        return sig if sig in proven_sides(self.meta) else 0

    def save(self, path=MODEL_PATH):
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps((self.model, self.edge, self.meta)))
        tmp.replace(path)

    def load(self, path=MODEL_PATH):
        try:
            self.model, self.edge, self.meta = pickle.loads(path.read_bytes())
            return True
        except Exception:
            return False

    @contextmanager
    def use(self, other):
        """Pakai model lain sementara (untuk backtest walk-forward)."""
        saved = (self.model, self.edge, self.meta)
        self.model, self.edge, self.meta = other.model, other.edge, other.meta
        try:
            yield
        finally:
            self.model, self.edge, self.meta = saved


AI = AiModel()
