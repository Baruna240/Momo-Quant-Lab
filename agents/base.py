"""Fondasi agent: hasil terstruktur + penanganan error yang seragam.

Setiap agent:
- punya satu tanggung jawab (`role`) dan daftar sumber data yang dibaca (`reads`)
- menerima `state` (hasil agent sebelumnya di siklus yang sama) dan mengembalikan AgentResult
- TIDAK PERNAH melempar error ke Supervisor: kegagalan dicatat di `errors`, agent lain tetap jalan
- hanya MEMBACA database (mode read-only), tidak mengirim order apa pun
"""
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import BASE_DIR


@dataclass
class AgentResult:
    agent: str
    ok: bool
    data: dict
    summary: str = ""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    started: float = 0.0
    took_ms: int = 0

    def to_dict(self):
        return asdict(self)


class Agent:
    name = "agent"
    role = ""
    reads = ()

    def __init__(self, base_dir=BASE_DIR):
        self.base = Path(base_dir)

    def run(self, state=None):
        t0 = time.time()
        try:
            data, summary, warnings = self.act(state or {})
            return AgentResult(self.name, True, data, summary, [], list(warnings), t0, int((time.time() - t0) * 1000))
        except Exception as e:      # satu agent gagal tidak boleh menghentikan siklus
            return AgentResult(self.name, False, {}, f"gagal: {type(e).__name__}", [f"{type(e).__name__}: {e}"], [], t0,
                               int((time.time() - t0) * 1000))

    def act(self, state):
        """Kembalikan (data: dict, ringkasan: str, peringatan: list)."""
        raise NotImplementedError

    def ro(self, filename):
        path = self.base / filename
        if not path.exists():
            raise FileNotFoundError(f"{filename} belum ada")
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def upstream(state, name):
        """Data agent sebelumnya di siklus ini (kosong kalau agent itu gagal / belum jalan)."""
        r = state.get(name) or {}
        return r.get("data") or {} if r.get("ok") else {}
