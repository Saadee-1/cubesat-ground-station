"""Start the ground station: python -m groundstation [options].

The Python version is checked on the very first lines, before anything below is imported, so an
older Python gets a plain message and exit code 2 instead of a SyntaxError traceback. 3.10 is the
version this program was written and tested with; older versions were not tried.
"""
import sys

MIN_PYTHON = (3, 10)


def python_problem(version_info=None):
    """A plain message when this Python is too old, else None."""
    version = tuple(version_info if version_info is not None else sys.version_info)
    if version[:2] >= MIN_PYTHON:
        return None
    return ("The CubeSat ground station needs Python %d.%d or newer, found %d.%d. %d.%d is the version it was "
            "written and tested with; older ones were not tried. Install it from python.org, or start the "
            "program with:  py -3.10 -m groundstation"
            % (MIN_PYTHON + tuple(version[:2]) + MIN_PYTHON))


if python_problem() is not None:  # checked first: nothing further down may run on an older Python
    sys.stderr.write(python_problem() + "\n")
    raise SystemExit(2)


def tkinter_problem():
    """A plain message when tkinter (the GUI toolkit) is missing, else None."""
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        return ("This Python has no tkinter (the Tk window toolkit): %s. Run the Python installer again, choose "
                "Modify, and tick \"tcl/tk and IDLE\". (Linux: install the python3-tk package.)" % exc)
    return None


def main(argv=None):
    """Check the environment, then run the application; returns the process exit code."""
    problem = tkinter_problem()  # the version was already checked by the guard at the top of this file
    if problem is not None:
        sys.stderr.write(problem + "\n")
        return 2
    from groundstation.app import main as app_main  # imported only after the checks above
    return app_main(argv)


if __name__ == "__main__":
    sys.exit(main())
