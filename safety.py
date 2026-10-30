"""Agent Safety: skor risiko berlapis untuk token pump.fun (0 = tidak ada tanda bahaya, 100 = sangat berisiko).

Prinsip: token TIDAK pernah dianggap aman hanya karena lolos satu pemeriksaan; setiap lapis menambah skor & alasan.
Lapis 1 - pembuat koin : dev serial, porsi token dev, nama tiruan narasi yang sedang ramai
Lapis 2 - peluncuran   : SOL dibeli di blok pertama (bundle), jumlah sniper, sniper "langganan", beli berukuran identik
Lapis 3 - aliran 60 dtk: dominasi satu pembeli, wash trading, tekanan jual, dev mulai menjual, sepi pembeli
Lapis 4 - holder       : (dari pemeriksaan anti-rug di sim.py: konsentrasi top1/top5) bila tersedia

Disesuaikan dengan pump.fun di Solana: mint & freeze authority token bonding curve dipegang program pump.fun
(bukan dev), dan penjualan selalu lewat program yang sama, jadi pemeriksaan honeypot/authority ala EVM tidak relevan
di sini. Untuk token di luar pump.fun pemeriksaan itu WAJIB ditambahkan sebelum dipakai.

Bobot di bawah adalah titik awal dan BELUM terbukti. Skor dicatat untuk setiap token di detik ke-60 lalu dibandingkan
dengan label 20 menit (flow.py: rug/dump/...). Baru setelah terbukti memisahkan rug, skor boleh dipakai sebagai filter.
"""


def assess(snap, ctx=None, missed_sol=None, holders=None):
    """snap: ringkasan aliran transaksi (flow.snapshot); ctx: ciri saat lahir (features.on_create);
    holders: {"top1": .., "top5": ..} bila ada. Hasil: (skor 0-100, [alasan])."""
    ctx = ctx or {}
    score, why = 0.0, []

    def add(points, reason):
        nonlocal score
        score += points
        why.append(reason)

    # lapis 1: pembuat koin
    if (ctx.get("creator_n24") or 0) >= 3:
        add(15, f"dev serial ({int(ctx['creator_n24'])} koin/24 jam)")
    if (ctx.get("dev_pct") or 0) >= 10:
        add(10, f"dev memegang {ctx['dev_pct']:.0f}% suplai")
    if (ctx.get("name_n1h") or 0) >= 2:
        add(5, "nama tiruan (narasi yang sedang ditiru)")
    # lapis 2: peluncuran
    if missed_sol is not None:
        if missed_sol >= 3:
            add(15, f"{missed_sol:.1f} SOL dibeli di blok pertama (bundle)")
        elif missed_sol >= 1:
            add(8, f"{missed_sol:.1f} SOL dibeli di blok pertama")
    if (snap.get("snipers") or 0) >= 5:
        add(8, f"{snap['snipers']} sniper dalam 3 detik")
    if (snap.get("serial_snipers") or 0) >= 2:
        add(8, f"{snap['serial_snipers']} sniper langganan (bot)")
    if (snap.get("same_size") or 0) >= 0.4 and (snap.get("n_buy") or 0) >= 5:
        add(10, f"{snap['same_size']:.0%} pembelian berukuran identik (bot/bundle)")
    # lapis 3: aliran transaksi
    if (snap.get("top1_share") or 0) >= 0.4:
        add(10, f"1 dompet = {snap['top1_share']:.0%} pembelian")
    if (snap.get("wash_share") or 0) >= 0.3:
        add(10, f"{snap['wash_share']:.0%} volume dari dompet yang beli & jual (wash)")
    if snap.get("buy_ratio") is not None and snap["buy_ratio"] <= 0.45 and (snap.get("n_buy", 0) + snap.get("n_sell", 0)) >= 5:
        add(8, f"tekanan jual (beli hanya {snap['buy_ratio']:.0%} volume)")
    if (snap.get("dev_sell_frac") or 0) >= 0.2:
        add(20, f"dev sudah menjual {snap['dev_sell_frac']:.0%}")
    if (snap.get("buyers") or 0) < 5:
        add(5, f"sepi pembeli ({snap.get('buyers') or 0} dompet)")
    # lapis 4: holder
    if holders:
        if (holders.get("top1") or 0) > 0.10:
            add(10, f"1 holder memegang {holders['top1']:.0%}")
        if (holders.get("top5") or 0) > 0.30:
            add(8, f"5 holder teratas memegang {holders['top5']:.0%}")
    return min(100.0, score), why
