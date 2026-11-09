"""``python -m eidos.apps.role <role>`` — start one lab process role (used by the supervisor)."""

import sys


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    role = argv[0]
    if role == "supervisor":
        from eidos.apps.roots import setup_logging
        from eidos.settings import LabConfig
        from eidos.supervisor.daemon import run_forever
        setup_logging("supervisor")
        run_forever(LabConfig.load())
        return
    from eidos.apps.roots import main_role
    main_role(role)


if __name__ == "__main__":
    main()
