# CLI Reference

## Training

```bash
python scripts/train.py --config <config.yaml> [overrides...]
```

### Arguments

| Argument | Description |
|---|---|
| `--config` | Path to YAML config file (required) |
| `key=value` | Override any config parameter |
| `--backend custom_chat` | Use a custom OpenAI-compatible `/chat/completions` endpoint |
| `--custom_chat_base_url` | Shared custom endpoint base URL |
| `--custom_chat_api_key` | Optional shared custom endpoint API key |
| `--custom_chat_model` | Shared custom model name |
| `--optimizer_custom_chat_*` / `--target_custom_chat_*` | Per-role custom endpoint/API key/model overrides |

### Examples

```bash
# Basic training
python scripts/train.py --config configs/searchqa/default.yaml

# With overrides
python scripts/train.py \
  --config configs/searchqa/default.yaml \
  --cfg-options optimizer.learning_rate=16 optimizer.lr_scheduler=linear

# With custom initial skill
python scripts/train.py \
  --config configs/searchqa/default.yaml \
  --cfg-options env.skill_init=skills/my_seed.md

# With a custom OpenAI-compatible model endpoint
python scripts/train.py \
  --config configs/searchqa/default.yaml \
  --backend custom_chat \
  --custom_chat_base_url http://localhost:8000/v1 \
  --custom_chat_api_key sk-local \
  --custom_chat_model your-model-name
```

## Evaluation

```bash
python scripts/eval_only.py --config <config.yaml> --skill <skill.md>
```

### Arguments

| Argument | Description |
|---|---|
| `--config` | Path to YAML config file (required) |
| `--skill` | Path to skill document to evaluate (required) |
| `--split` | Evaluation split: `test` (default), `valid`, `train` |

### Examples

```bash
# Evaluate best skill on test set
python scripts/eval_only.py \
  --config configs/searchqa/default.yaml \
  --skill outputs/searchqa/run_001/skills/best_skill.md

# Evaluate on validation set
python scripts/eval_only.py \
  --config configs/searchqa/default.yaml \
  --skill outputs/searchqa/run_001/skills/best_skill.md \
  --split valid
```

## WebUI

```bash
python -m skillopt_webui.app [--port PORT] [--share]
```

| Argument | Default | Description |
|---|---|---|
| `--port` | 7860 | Port number |
| `--share` | false | Create public Gradio link |
