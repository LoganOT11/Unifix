# CLAUDE.md

## Conda Environment

This project uses the `unifix` conda environment.

```bash
# Activate the environment
conda activate unifix

# Deactivate when done
conda deactivate
```

The environment runs Python 3.12. Dependencies are tracked in two files:

- **`environment.yml`** — Conda environment spec (conda packages + pip dependencies). Rebuild the full environment with:
  ```bash
  conda env create -f environment.yml
  ```

- **`requirements.txt`** — Pip-only dependencies. Install into an already-active conda env with:
  ```bash
  pip install -r requirements.txt
  ```

### Adding New Dependencies

When you add a new package, update the tracking files:

```bash
# After conda install:
conda env export --no-builds | grep -v "^prefix:" > environment.yml

# After pip install:
pip freeze > requirements.txt
```

Or manually add the package to the appropriate file.

## API Keys

Copy `.env` and fill in your keys. The `.env` file is gitignored and never committed.
