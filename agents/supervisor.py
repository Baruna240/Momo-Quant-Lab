"""Agent Supervisor: mengatur alur 7 agent spesialis dengan LangGraph dan mencatat setiap siklus ke agents.db.

Jalankan: python -m agents.supervisor --once          (satu siklus, cetak ringkasan)
          python -m agents.supervisor --loop 600      (siklus tiap 10 menit; Challenger sekali sehari)
          python -m agents.supervisor --once --challenge

Alur (graf LangGraph, setiap node = kode deterministik, tidak ada LLM di jalur keputusan):
  meme_scanner -> safety -> altcoin_scanner -> market_regime -> smart_money -> risk -> research
Satu agent gagal tidak menghentikan siklus (AgentResult.ok = False, agent berikutnya tetap jalan).
Kalau LangGraph tidak terpasang, node yang sama dijalankan berurutan tanpa graf.
"""
import argparse
import json
import sqlite3
import sys
import time
from typing import Any, TypedDict

from agents.base import Agent
from agents.guards import MarketRegimeAgent, RiskAgent, SafetyAgent
from agents.research_agents import ResearchAgent, SmartMoneyAgent
from agents.scanners import AltcoinScannerAgent, MemeScannerAgent
from config import BASE_DIR

AGENTS_DB = BASE_DIR / "agents.db"
ORDER = [MemeScannerAgent, SafetyAgent, AltcoinScannerAgent, MarketRegimeAgent, SmartMoneyAgent, RiskAgent, ResearchAgent]
CHALLENGE_EVERY = 24 * 3600


class CycleState(TypedDict, total=False):
    challenge: bool
    proposals: list
    meme_scanner: dict
    safety: dict
    altcoin_scanner: dict
    market_regime: dict
    smart_money: dict
    risk: dict
    research: dict


def _node(agent):
    def run(state: CycleState) -> dict[str, Any]:
        return {agent.name: agent.run(dict(state)).to_dict()}
    run.__name__ = agent.name
    return run


def build_graph(agents):
    """Graf LangGraph berurutan; None kalau LangGraph tidak tersedia (dipakai runner sederhana)."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None
    g = StateGraph(CycleState)
    for a in agents:
        g.add_node(a.name, _node(a))
    g.add_edge(START, agents[0].name)
    for a, b in zip(agents, agents[1:]):
        g.add_edge(a.name, b.name)
    g.add_edge(agents[-1].name, END)
    return g.compile()


class Supervisor(Agent):
    name = "supervisor"
    role = "Mengatur alur 7 agent & mencatat setiap siklus"
    reads = ("semua agent", "agents.db")

    def __init__(self, base_dir=BASE_DIR, agents=None, db_path=None):
        super().__init__(base_dir)
        self.agents = agents or [cls(base_dir) for cls in ORDER]
        self.db_path = db_path or AGENTS_DB
        self.graph = build_graph(self.agents)

    def cycle(self, challenge=False, proposals=()):
        t0 = time.time()
        state = {"challenge": challenge, "proposals": list(proposals)}
        if self.graph is not None:
            state = self.graph.invoke(state)
            engine = "langgraph"
        else:
            for a in self.agents:
                state[a.name] = a.run(dict(state)).to_dict()
            engine = "berurutan"
        results = [state[a.name] for a in self.agents if a.name in state]
        ok = sum(r["ok"] for r in results)
        summary = {"agent": self.name, "ok": ok == len(results), "data": {"engine": engine, "agents_ok": ok, "agents": len(results)},
                   "summary": f"{ok}/{len(results)} agent sehat · mesin {engine}", "errors": [e for r in results for e in r["errors"]],
                   "warnings": [], "started": t0, "took_ms": int((time.time() - t0) * 1000)}
        self.save(t0, results + [summary])
        return state, summary

    def save(self, ts, results):
        db = sqlite3.connect(self.db_path)
        db.execute("""CREATE TABLE IF NOT EXISTS agent_runs (cycle REAL, agent TEXT, ok INTEGER, took_ms INTEGER, summary TEXT,
                      errors TEXT, warnings TEXT, data TEXT)""")
        db.executemany("INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?)", [
            (ts, r["agent"], int(r["ok"]), r["took_ms"], r["summary"], json.dumps(r["errors"]), json.dumps(r["warnings"]),
             json.dumps(r["data"], default=float)[:200000]) for r in results])
        db.execute("DELETE FROM agent_runs WHERE cycle < ?", (ts - 14 * 86400,))   # simpan 14 hari terakhir
        db.commit()
        db.close()


def latest_cycle(path=AGENTS_DB):
    """Hasil siklus terakhir untuk dashboard: {agent: {ok, summary, took_ms, errors, warnings}}, waktu siklus."""
    if not path.exists():
        return {}, None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ts = db.execute("SELECT MAX(cycle) FROM agent_runs").fetchone()[0]
    rows = db.execute("SELECT agent, ok, took_ms, summary, errors, warnings FROM agent_runs WHERE cycle = ?", (ts,)).fetchall() if ts else []
    db.close()
    return {r[0]: {"ok": bool(r[1]), "took_ms": r[2], "summary": r[3], "errors": json.loads(r[4]), "warnings": json.loads(r[5])}
            for r in rows}, ts


def last_challenge_time(path=AGENTS_DB):
    """Kapan Challenger terakhir dijalankan (diingat lintas restart supaya jatah percobaan tidak terbuang)."""
    if not path.exists():
        return 0.0
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = db.execute("""SELECT MAX(cycle) FROM agent_runs WHERE agent='research' AND data LIKE '%"challengers"%'""").fetchone()
    except sqlite3.OperationalError:
        row = None
    db.close()
    return (row[0] or 0.0) if row else 0.0


def print_cycle(state, summary, agents):
    for a in agents:
        r = state.get(a.name)
        if not r:
            continue
        mark = "✓" if r["ok"] else "✗"
        print(f"{mark} {a.name:<16} {r['took_ms']:>6} ms  {r['summary']}")
        for e in r["errors"]:
            print(f"     error: {e}")
        for w in r["warnings"][:3]:
            print(f"     catatan: {w}")
    print(f"= {summary['summary']} · {summary['took_ms']} ms")


def main():
    ap = argparse.ArgumentParser(description="Supervisor 8 agent (LangGraph)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="detik antar siklus")
    ap.add_argument("--challenge", action="store_true", help="jalankan Champion vs Challenger di siklus ini")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sup = Supervisor()
    last_challenge = last_challenge_time()
    while True:
        do_ch = args.challenge or (args.loop and time.time() - last_challenge >= CHALLENGE_EVERY)
        state, summary = sup.cycle(challenge=bool(do_ch))
        if do_ch:
            last_challenge = time.time()
        print(time.strftime("%H:%M:%S"))
        print_cycle(state, summary, sup.agents)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
