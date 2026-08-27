"""Allow ``python -m personal_ai_os`` as well as the ``paios`` entry point."""

from personal_ai_os.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
