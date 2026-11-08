# llama-server 放这里

`tools/llama/` 的骨架已在仓库里，二进制不入库（约 100 MB，且按平台/后端分版本）。

在本机跑 7B 模型（预问诊默认走本机模型）时下载：

```bash
python scripts/get_llama.py                 # 自动选：Windows 优先 Vulkan
python scripts/get_llama.py --backend cpu   # 没有独显 / 想最小体积
python scripts/get_llama.py --list          # 只看这个 tag 有哪些资产
```

脚本只用标准库，会查最新 tag、挑对平台资产、直连失败时走镜像并就地解压。`config/models.yaml` 里 `local` 后端的 `path` 是开发机绝对路径，用本机模型时改成你自己的 `.gguf`；不用本机模型则整步跳过，走云端后端即可。
