test:
  ruff check src
  mypy -p src
  pytest tests

format:
  ruff check src tests --select I001 --fix
  ruff format src tests
