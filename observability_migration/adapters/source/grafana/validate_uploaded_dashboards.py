"""Package-level entrypoint for uploaded dashboard validation."""

from .smoke import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
