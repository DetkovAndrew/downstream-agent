# Repository Guidelines

## Project Structure & Module Organization

`downstream_agent/` contains the Python package: collection and discovery, news processing, PDF recognition, LLM providers, and Excel generation. The local application server is `downstream_agent/app.py`; its browser interface is `downstream_agent/web/index.html`. `app_entry.py` is the Windows executable entry point, and `ui.py` contains the Streamlit interface. `companies.toml` defines company names, aliases, languages, and sources.

`data/` holds collected documents, news, cached results, and generated workbooks; it is created at run time and is not tracked.

## Build, Test, and Development Commands

- `python -m pip install -r requirements-server.txt` installs the server dependencies.
- `python -m downstream_agent.app` starts the local application; it normally opens a browser.
- `python -m downstream_agent --help` lists command-line operations; add `--help` to a subcommand for its options.
- `build_exe.bat` builds `dist/Downstream-Agent.exe` on Windows using PyInstaller and `.winbuild/`. Rebuild the executable to include source changes.

## Coding Style & Naming Conventions

Follow the existing Python style: four-space indentation, `snake_case` for functions and variables, `PascalCase` for classes, and uppercase constants. Use descriptive module names and type annotations where practical. Keep Russian user-facing text consistent with the existing interface. No repository-wide formatter or lint configuration is present.

## Testing Guidelines

No automated test suite or coverage requirement is defined. For changes, check the relevant CLI or application workflow and inspect generated Excel files. Use isolated temporary data and stub external services for repeatable checks; separately verify real providers when integration testing is requested. Avoid overwriting the user's accumulated data during checks.

## Commits & Pull Requests

Git history is unavailable in this workspace, so no established commit convention can be verified. Use a short, descriptive commit subject. Pull requests should explain the behavior changed, list checks performed, link any relevant issue, and include screenshots for interface changes.

## Security & Configuration

Use `.env.example` as a configuration template and keep real keys out of commits and logs. `build_exe.bat` embeds `.env` when present, so treat the resulting executable as containing credentials. External providers and full integration checks can consume API quota.
