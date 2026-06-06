"""Consumer-side Sensor persistence — pickle a trained Limen Sensor to disk
with a version sidecar, and reload it only when the running env matches.

A Sensor carries a lazily-built ``_manifest`` (a ``CompiledSFD`` whose scaler
factory is a closure stdlib pickle cannot serialize). ``persist_sensor`` nulls
that cache before pickling; the manifest rebuilds itself from the pickled
``_yaml_reference`` on the first predict after reload, so the round-trip is
bit-identical. A version sidecar records the ``limen`` + ``scikit-learn``
versions the pickle was written under; ``load_sensor`` refuses a pickle whose
versions differ from the running env — it returns ``None`` so the caller
regenerates via ``Trainer.train`` rather than unpickling a model across an
incompatible version boundary.

This keeps a serving process from re-training every cohort member on every
start: a Sensor is trained once, persisted, and reloaded zero-copy on the next
boot as long as the env has not moved underneath it.
"""

from __future__ import annotations

import json
import pickle
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import cast

from limen.experiment import Sensor


def env_versions() -> dict[str, str]:
    """The ``limen`` + ``scikit-learn`` versions a pickle is keyed to. A
    Sensor pickle is only safe to reload under the same pair (the model is an
    sklearn estimate; the manifest is rebuilt by limen)."""
    return {
        "limen_version": _package_version("vaquum-limen"),
        "sklearn_version": _package_version("scikit-learn"),
    }


def _pickle_path(directory: Path, permutation_id: str) -> Path:
    return directory / f"{permutation_id}.pkl"


def _sidecar_path(directory: Path, permutation_id: str) -> Path:
    return directory / f"{permutation_id}.json"


def persist_sensor(sensor: Sensor, directory: Path) -> str:
    """Pickle ``sensor`` to ``<directory>/<permutation_id>.pkl`` with a
    version sidecar, returning the permutation_id. ``_manifest`` is nulled
    first — it is a Limen-private lazy ``CompiledSFD`` whose scaler-factory
    closure stdlib pickle rejects; nulling it makes the pickle succeed and the
    manifest rebuilds from ``_yaml_reference`` on the next predict. The
    private attribute is reached via ``setattr`` so pyright's name-keyed
    ``reportPrivateUsage`` stays satisfied without suppressing it. Both files
    are written atomically (temp + ``os.replace``)."""
    permutation_id = sensor.permutation_id
    if not permutation_id:
        raise ValueError("sensor has no permutation_id; cannot persist")
    directory.mkdir(parents=True, exist_ok=True)
    # Drop the unpicklable lazy manifest cache (sensor._manifest = None).
    setattr(sensor, "_manifest", None)

    pkl = _pickle_path(directory, permutation_id)
    pkl_tmp = pkl.with_name(pkl.name + ".tmp")
    with pkl_tmp.open("wb") as f:
        pickle.dump(sensor, f, protocol=pickle.HIGHEST_PROTOCOL)
    pkl_tmp.replace(pkl)

    sidecar: dict[str, object] = {
        **env_versions(),
        "manifest_id": sensor.manifest_id,
        "permutation_id": permutation_id,
    }
    sidecar_path = _sidecar_path(directory, permutation_id)
    sidecar_tmp = sidecar_path.with_name(sidecar_path.name + ".tmp")
    sidecar_tmp.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    sidecar_tmp.replace(sidecar_path)
    return permutation_id


def load_sensor(directory: Path, permutation_id: str) -> Sensor | None:
    """Return the persisted Sensor when its version sidecar matches the
    running env, else ``None`` (the caller regenerates via ``Trainer.train``).
    A missing pickle or sidecar also returns ``None``. A version mismatch is
    not an error — it is the regenerate signal — but a pickle that exists yet
    deserializes to a non-Sensor is a corrupted store and fails loud."""
    pkl = _pickle_path(directory, permutation_id)
    sidecar = _sidecar_path(directory, permutation_id)
    if not pkl.is_file() or not sidecar.is_file():
        return None

    recorded_obj: object = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(recorded_obj, dict):
        return None
    recorded = cast(dict[object, object], recorded_obj)
    for key, value in env_versions().items():
        if recorded.get(key) != value:
            return None

    with pkl.open("rb") as f:
        loaded: object = pickle.load(f)
    if not isinstance(loaded, Sensor):
        raise ValueError(f"persisted object at {pkl} is not a limen Sensor")
    return loaded
