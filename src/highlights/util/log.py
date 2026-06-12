"""Shared rich console for CLI output."""

from rich.console import Console

console = Console()


def info(message: str) -> None:
    console.print(message)


def warn(message: str) -> None:
    console.print(f"[yellow]Warning:[/yellow] {message}")


def error(message: str) -> None:
    console.print(f"[red]Error:[/red] {message}")
