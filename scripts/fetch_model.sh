#!/usr/bin/env bash
# 从 HuggingFace 镜像下载模型到本地目录，避开国内直连必挂的问题。
#
#   ./scripts/fetch_model.sh BAAI/bge-small-zh-v1.5
#   ./scripts/fetch_model.sh BAAI/bge-reranker-base
#
# 目标目录：data/models/<org--name>，与 knowtune 的自动查找规则一致。
# 用 curl 而不是 huggingface-cli：新版 hf 走 Xet 存储，镜像对 Xet 支持不完整，
# 会出现"下载成功但文件是空的"，排查起来非常费劲。
set -uo pipefail

MODEL="${1:-BAAI/bge-small-zh-v1.5}"
BASE="${HF_ENDPOINT:-https://hf-mirror.com}"
SLUG="$(echo "$MODEL" | tr '/' '-')"
SLUG="${MODEL//\//--}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/data/models/$SLUG"

mkdir -p "$DEST/1_Pooling"
echo "模型:  $MODEL"
echo "镜像:  $BASE"
echo "目标:  $DEST"
echo

SMALL_FILES=(
  config.json
  config_sentence_transformers.json
  modules.json
  sentence_bert_config.json
  tokenizer_config.json
  tokenizer.json
  vocab.txt
  special_tokens_map.json
  1_Pooling/config.json
)

fail=0
for f in "${SMALL_FILES[@]}"; do
  code=$(curl -sL --max-time 60 -w "%{http_code}" -o "$DEST/$f" \
    "$BASE/$MODEL/resolve/main/$f")
  if [ "$code" = "200" ] && [ -s "$DEST/$f" ]; then
    printf '  ok   %-34s %s\n' "$f" "$(du -h "$DEST/$f" | cut -f1)"
  else
    rm -f "$DEST/$f"   # 404 会留下一个内容是错误页的小文件，必须删掉
    if [ "$f" = "1_Pooling/config.json" ] || [ -f "$DEST/config.json" ]; then
      printf '  skip %-34s (fatal=%s)\n' "$f" "$code"
    else
      printf '  FAIL %-34s (http %s)\n' "$f" "$code"
      fail=1
    fi
  fi
done

echo
for w in model.safetensors pytorch_model.bin; do
  echo "下载权重 $w ..."
  code=$(curl -sL --max-time 900 -w "%{http_code}" -o "$DEST/$w" \
    "$BASE/$MODEL/resolve/main/$w")
  if [ "$code" = "200" ] && [ -s "$DEST/$w" ]; then
    echo "  完成 $(du -h "$DEST/$w" | cut -f1)"
    break
  else
    rm -f "$DEST/$w"
    echo "  无 $w (http $code)"
  fi
done

if [ ! -f "$DEST/config.json" ]; then
  echo
  echo "失败：config.json 没有下载成功，检查镜像是否可达："
  echo "  curl -I $BASE/$MODEL/resolve/main/config.json"
  exit 1
fi

if ! ls "$DEST"/model.safetensors "$DEST"/pytorch_model.bin >/dev/null 2>&1; then
  echo
  echo "失败：权重文件缺失"
  exit 1
fi

echo
echo "完成。在 config/default.yaml 中设置："
if [[ "$MODEL" == *reranker* ]]; then
  echo "  retrieval.rerank.local_path: ./data/models/$SLUG"
  echo "  （或直接留空，程序会按 data/models/$SLUG 自动查找）"
else
  echo "  embedding.local_path: ./data/models/$SLUG"
fi
