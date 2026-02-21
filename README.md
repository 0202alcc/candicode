# CandiCode

Custom pipeline + native OpenCode integration to run a multi-phase software-engineering workflow (`intent -> triage -> requirements -> plan -> code -> test -> review -> docs -> execute -> verify -> gates`).

This repo **does not vendor** OpenCode core.  
Instead, it includes a patch file for upstream OpenCode:

- `patches/opencode-core-custom.patch`

## Security

- Do **not** commit credentials or auth files.
- This repo ignores runtime/auth-local artifacts via `.gitignore`.
- Keep provider auth in OpenCode auth storage (`opencode auth ...`) or local env at runtime only.

## Quick Setup

1. Clone this repo.
2. Clone OpenCode core separately.
3. Apply the patch.
4. Build OpenCode core.
5. Point `candicode` command to the patched binary + this pipeline entrypoint.

### 1) Clone

```bash
git clone https://github.com/0202alcc/candicode.git
cd candicode
```

### 2) Clone OpenCode Core

```bash
git clone https://github.com/anomalyco/opencode.git opencode-core
```

### 3) Apply Patch

```bash
git -C opencode-core apply ../patches/opencode-core-custom.patch
```

### 4) Build OpenCode Core

```bash
cd opencode-core/packages/opencode
PATH="$HOME/.bun/bin:$PATH" ~/.bun/bin/bun run build
cd ../../..
```

### 5) Shell Command (`candicode`)

Add this to your shell rc (`~/.zshrc` / `~/.bashrc`):

```bash
candicode() {
  OPENCODE_BIN_PATH="/absolute/path/to/candicode/opencode-core/packages/opencode/dist/opencode-darwin-arm64/bin/opencode" \
  OPENCODE_PIPELINE_ENTRYPOINT="/absolute/path/to/candicode/scripts/opencode_entrypoint.py" \
  opencode "$@"
}
```

Notes:
- Adjust binary path for your platform (`darwin-arm64`, `linux-x64`, etc).
- Keep `opencode` unmodified; use `candicode` for patched flow.

## Python Environment (uv)

`uv` is recommended for cross-platform consistency, even though current dependencies are minimal.

```bash
uv sync
uv run python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Docker (optional)

Docker is useful for running tests consistently in CI, but not for interactive local OpenCode TUI workflows.

```bash
docker build -t candicode .
docker run --rm candicode
```

## Using Provider Mode

- `/model` in OpenCode should select provider/model.
- `opencode auth <provider>` should manage credentials.
- This pipeline receives provider/model/credentials via native integration bridge.

## Useful Commands

```bash
python3 scripts/opencode_entrypoint.py "Fix parser edge case" --model-client auto
python3 scripts/validate_backend_contract_v1.py
python3 scripts/run_pipeline_scenarios.py
python3 -m unittest discover -s tests -p "test_*.py" -v
```
