from pathlib import Path


def test_eggopt_package_does_not_access_storage_connections_directly():
    package = Path(__file__).resolve().parents[1] / "eggopt"
    offenders = []
    for path in package.rglob("*.py"):
        source = path.read_text()
        if ".conn" in source:
            offenders.append(str(path.relative_to(package.parent)))
    assert offenders == []
