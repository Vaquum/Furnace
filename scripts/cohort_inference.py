#!/usr/bin/env python3
"""Cohort inference helper.

Builds a Limen ``Cohort`` from one completed round's ``limen_output``
directory, feeds it the manifest-prescribed input data at a chosen
datetime, and prints either the input payload or the cohort prediction
to stdout as JSON.

This helper lives outside the typed ``furnace/`` package because it
imports ``limen`` and ``polars`` at module scope; both carry loose type
information and would otherwise grow the pyright error budget. Furnace
shells out to this script the same way it shells out to ``limen run``
and ``limen validate``.

Sensors are reconstructed anew on every invocation. No in-process cache
and no pickled artefact on disk — the (round, permutation_ids, datetime)
triple is the full key, and the manifest pins everything downstream.

Usage:

    python -m scripts.cohort_inference \\
        --limen-output PATH \\
        --permutation-ids 1,2,3 \\
        --at 2026-05-24T00:00:00 \\
        --mode {input,predict}
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from limen.cohort import Cohort
from limen.experiment import Trainer


def _parse_at(value: str) -> datetime:
    """Parse the --at argument as ISO 8601. Limen klines are
    timezone-naive UTC; we coerce any tz-aware input to naive UTC so
    the equality check against the test window's datetimes succeeds."""
    normalized = value.replace('Z', '+00:00') if value.endswith('Z') else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _parse_permutation_ids(value: str) -> list[int]:
    pieces = [s.strip() for s in value.split(',') if s.strip()]
    if not pieces:
        raise SystemExit('--permutation-ids must list at least one integer id')
    out: list[int] = []
    for s in pieces:
        try:
            out.append(int(s))
        except ValueError as exc:
            raise SystemExit(
                f'--permutation-ids entry {s!r} is not an integer'
            ) from exc
    return out


def _load_round_params(
    limen_output: Path,
    permutation_ids: list[int],
) -> dict[int, dict]:
    path = limen_output / 'round_data.jsonl'
    entries: dict[int, dict] = {}
    with path.open('r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            rid = obj.get('round_id')
            rp = obj.get('round_params')
            if isinstance(rid, int) and isinstance(rp, dict):
                entries[rid] = rp
    missing = [p for p in permutation_ids if p not in entries]
    if missing:
        raise SystemExit(
            f'permutation ids missing from round_data.jsonl: {missing}'
        )
    return {p: entries[p] for p in permutation_ids}


def _row_at(prepared: dict, at: datetime) -> pl.DataFrame:
    pdb = prepared.get('price_data_for_backtest')
    if not isinstance(pdb, pl.DataFrame):
        raise SystemExit(
            "manifest's prepare_data did not produce price_data_for_backtest; "
            'inference at a chosen datetime requires the OHLCV columns so the '
            'row-to-datetime mapping survives the transform drops'
        )
    if 'datetime' not in pdb.columns:
        raise SystemExit("price_data_for_backtest has no 'datetime' column")
    indexed = pdb.with_row_index('_row_idx').filter(
        pl.col('datetime') == pl.lit(at)
    )
    if indexed.height == 0:
        first = pdb['datetime'].min()
        last = pdb['datetime'].max()
        raise SystemExit(
            f'datetime {at.isoformat()} is not in the test window of this '
            f'round (test runs from {first} to {last}); pick a datetime '
            'inside that window'
        )
    if indexed.height > 1:
        raise SystemExit(
            f'datetime {at.isoformat()} matched {indexed.height} rows in '
            'the test window — round data is not unique-keyed by datetime'
        )
    idx_value = indexed['_row_idx'][0]
    if not isinstance(idx_value, int):
        raise SystemExit(f'row index returned non-int: {idx_value!r}')
    x_test = prepared.get('x_test')
    if not isinstance(x_test, pl.DataFrame):
        raise SystemExit(
            "manifest's prepare_data did not return x_test as a DataFrame"
        )
    return x_test.slice(idx_value, 1)


def _prepare_payload(trainer: Trainer, round_params: dict, at: datetime) -> pl.DataFrame:
    prepared = trainer._manifest.prepare_data(trainer._data, round_params)
    if not isinstance(prepared, dict):
        raise SystemExit('manifest.prepare_data did not return a dict')
    return _row_at(prepared, at)


def _row_to_records(row: pl.DataFrame) -> list[dict]:
    records: list[dict] = []
    for raw in row.iter_rows(named=True):
        records.append({str(k): v for k, v in raw.items()})
    return records


def _scalar_or_list(payload: object) -> object:
    if hasattr(payload, 'tolist'):
        flat = payload.tolist()
        if isinstance(flat, list) and len(flat) == 1:
            return flat[0]
        return flat
    return payload


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str))
    sys.stdout.write('\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limen-output', required=True, type=Path)
    parser.add_argument('--permutation-ids', required=True)
    parser.add_argument('--at', required=True)
    parser.add_argument('--mode', choices=['input', 'predict'], required=True)
    args = parser.parse_args()

    limen_output: Path = args.limen_output
    if not limen_output.is_dir():
        raise SystemExit(f'limen_output is not a directory: {limen_output}')

    at = _parse_at(args.at)
    pids = _parse_permutation_ids(args.permutation_ids)
    round_params_by_pid = _load_round_params(limen_output, pids)

    trainer = Trainer(limen_output)

    if args.mode == 'input':
        payloads_records: dict[str, list[dict]] = {}
        for pid in pids:
            row = _prepare_payload(trainer, round_params_by_pid[pid], at)
            payloads_records[str(pid)] = _row_to_records(row)
        _emit({
            'at': at.isoformat(),
            'permutation_ids': pids,
            'payloads': payloads_records,
        })
        return 0

    sensors = trainer.train(pids)
    payloads: dict[int, dict] = {}
    for pid in pids:
        row = _prepare_payload(trainer, round_params_by_pid[pid], at)
        payloads[pid] = {'x_test': row}
    cohort = Cohort(
        experiment_log_path=str(limen_output),
        permutation_ids=list(pids),
    )
    cohort.set_members(sensors)
    result = cohort.predict({'_by_permutation_id': payloads})
    if not isinstance(result, dict):
        raise SystemExit('Cohort.predict returned a non-dict payload')

    body: dict[str, object] = {
        'at': at.isoformat(),
        'permutation_ids': pids,
        'aggregation_mode': cohort.aggregation_mode,
        'prediction': _scalar_or_list(result.get('_preds')),
    }
    probs = result.get('_probs')
    if probs is not None:
        body['probability'] = _scalar_or_list(probs)
    _emit(body)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
