"""Check installed metadata offline, without importing GPU libraries or running pip."""

import argparse
from importlib import metadata
from pathlib import Path

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name


def missing_requirements(requirements, *, dependencies=True):
    pending = [(Requirement(value), {""}) for value in requirements]
    checked = set()
    problems = set()
    while pending:
        req, parent_extras = pending.pop()
        if req.marker and not any(req.marker.evaluate({"extra": extra}) for extra in parent_extras):
            continue
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            problems.add(f"{req.name}: missing")
            continue
        if not req.specifier.contains(dist.version, prereleases=True):
            problems.add(f"{req.name}: installed {dist.version}, need {req.specifier}")
            continue
        key = (canonicalize_name(req.name), frozenset(req.extras))
        if not dependencies or key in checked:
            continue
        checked.add(key)
        extras = {"", *req.extras}
        pending.extend((Requirement(value), extras) for value in dist.requires or [])
    return sorted(problems)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--require", action="append", default=[])
    parser.add_argument("--no-deps", action="store_true")
    args = parser.parse_args()
    requirements = list(args.require)
    for filename in args.files:
        requirements.extend(
            line.strip() for line in Path(filename).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    problems = missing_requirements(requirements, dependencies=not args.no_deps)
    for problem in problems:
        print(f"==> {problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
