"""hospitalprice - Ohio hospital price transparency research pipeline."""
from __future__ import annotations
import json

import typer
from rich.console import Console
from rich.table import Table

from .config import DB_PATH, FIGURES, SEED, TABLES, free_bytes, user_agent

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Ohio hospital price transparency: compliance + dispersion.")
con_ = Console()


def _emit(obj):
    con_.print_json(json.dumps(obj, default=str))


@app.command()
def frame(refresh: bool = typer.Option(False, help="Re-download CMS source files")):
    """Build the sample frame from CMS enrollment + Hospital General Information."""
    from .stages import frame as m
    n = m.build(refresh=refresh)
    con_.print(f"[bold]frame:[/bold] {n} Ohio facilities (unit = CCN)")


@app.command()
def domains(load: str = typer.Option(None, help="Ingest a completed manual queue CSV")):
    """Resolve hospital -> web domain. No CMS dataset contains a website."""
    from .stages import domains as m
    if load:
        con_.print(f"loaded {m.load_manual(load)} manual domain resolutions")
    else:
        _emit(m.resolve())


@app.command()
def discover(limit: int = typer.Option(None, help="Only probe the N largest domains"),
             restart: bool = typer.Option(False, help="Discard prior results and re-probe all")):
    """Find MRF URLs: cms-hpt.txt first, then footer scan. Resumes by default."""
    from .stages import discover as m
    _emit(m.run(limit=limit, resume=not restart))


@app.command()
def fetch(limit: int = typer.Option(None), extract: bool = typer.Option(True),
          all_: bool = typer.Option(False, "--all", help="Re-fetch already-parsed files")):
    """Download, hash, profile, and stream-extract basket prices in one pass."""
    from .stages import fetch as m
    _emit(m.run(limit=limit, extract=extract, only_missing=not all_))


@app.command()
def score(kappa: bool = typer.Option(False, help="Report manual-vs-automated agreement")):
    """Apply the compliance rubric at both attribution rules."""
    from .stages import score as m
    _emit(m.kappa() if kappa else m.run())


@app.command()
def extract():
    """Assemble the preregistered basket from parsed rows."""
    from .stages import extract as m
    _emit(m.run())


@app.command()
def normalize(
    load: str = typer.Option(None, help="Ingest a completed payer review CSV"),
    reviewer: str = typer.Option("unknown"),
    flag: bool = typer.Option(False, help="Run the plan-type rate-level screen"),
):
    """Payer crosswalk + manual review queue. Similarity proposes; it never merges."""
    from .stages import normalize as m
    if load:
        con_.print(f"appended {m.load_reviewed(load, reviewer)} decisions to decisions.jsonl")
    elif flag:
        _emit(m.flag_plan_types())
    else:
        _emit(m.build())


@app.command()
def analyze(balance: bool = typer.Option(False, help="Discloser vs non-discloser balance table")):
    """Dispersion, placebo test, fragility check, and regressions."""
    from .stages import analyze as m
    if balance:
        con_.print(m.balance_table().to_string(index=False))
    else:
        _emit(m.run())


@app.command()
def export():
    """Publication tables, figures, and the public CSV."""
    from .stages import export as m
    _emit(m.run())


@app.command()
def medicare(csv: str = typer.Option(None, help="Benchmark CSV: code,code_type,setting,national_rate"),
             drg_weights: str = typer.Option(None, help="IPPS Table 5 CSV: drg,weight"),
             year: int = typer.Option(2025)):
    """Load Medicare benchmark rates (required for commercial-to-Medicare ratios)."""
    from .reference import medicare as m
    if csv:
        con_.print(f"loaded {m.load_csv(csv, year, 'manual CSV')} benchmark rows")
    if drg_weights:
        con_.print(f"loaded {m.load_drg_from_weights(drg_weights, year)} DRG rows")
    _emit(m.status())


@app.command()
def publish():
    """Export out/site_snapshot.json for reedsmoneymaker23.com/hospitals."""
    from . import publish as m
    _emit(m.build())


@app.command()
def status():
    """Where the pipeline stands, and the attrition table so far."""
    from .db import connect
    con = connect(True)
    t = Table(title="hospitalprice status", header_style="bold")
    t.add_column("stage"); t.add_column("step"); t.add_column("n", justify="right")
    t.add_column("of", justify="right"); t.add_column("note", overflow="fold", max_width=52)
    for r in con.execute(
        "SELECT stage, step, n, denominator, note FROM attrition "
        "QUALIFY row_number() OVER (PARTITION BY stage, step ORDER BY recorded_at DESC)=1 "
        "ORDER BY stage, step").fetchall():
        t.add_row(r[0], r[1], str(r[2]), str(r[3] or ""), (r[4] or "")[:200])
    con_.print(t)
    con.close()
    con_.print(f"\ndb={DB_PATH}  seed={SEED}  free_disk={free_bytes()/1e9:.1f} GB")
    try:
        con_.print(f"user-agent: {user_agent()}")
    except RuntimeError as e:
        con_.print(f"user-agent: not set ({e})")


@app.command()
def run_all(limit: int = typer.Option(None)):
    """frame -> domains -> discover -> fetch -> score -> extract -> normalize -> analyze -> export."""
    from .stages import (analyze as A, discover as D, domains as DM, export as E,
                         extract as X, fetch as F, frame as FR, normalize as N,
                         score as S)
    con_.print("[bold]1[/bold] frame");     con_.print(FR.build())
    con_.print("[bold]2[/bold] domains");   _emit(DM.resolve())
    con_.print("[bold]3[/bold] discover");  _emit(D.run(limit=limit))
    con_.print("[bold]4[/bold] fetch");     _emit(F.run(limit=limit))
    con_.print("[bold]5[/bold] score");     _emit(S.run())
    con_.print("[bold]6[/bold] extract");   _emit(X.run())
    con_.print("[bold]7[/bold] normalize"); _emit(N.build())
    con_.print("[bold]8[/bold] analyze");   _emit(A.run())
    con_.print("[bold]9[/bold] export");    _emit(E.run())


if __name__ == "__main__":
    app()
