# 第二大脑工作约定

这是本地的个人信息分拣器。实施契约以 `docs/BUILD_PLAN_CN_v3.md` 为准。本文件用自己的话记录代理该遵守的边界，不复制外部原文。

方法来源是 Andrej Karpathy 的想法说明：https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

这里只采用三层想法：raw 是不覆盖的证据，wiki 是可重新生成的阅读页，schema 规定代理怎么更新它们。该说明不是本仓库的许可证，也不把任何外部仓库当成要复制的实现。

非共识页只对照库内句子的分布。它不是市场结论，也不是买卖、仓位或目标价。

## 硬边界

- 运行依赖只有 feedparser 6.0.14 与 PyYAML 6.0.3。测试依赖 pytest 9.2.0。不要加入 Web 框架、向量库、Whisper、yt-dlp、Torch 或播放器。
- 测试零出网。不要请求 B 站 API，不要下载音频或画面，不要调用 ffmpeg。
- 不要读取或写入凭据。将来如果必须使用密钥，只从环境变量的名字读取，不要把秘密写进仓库。
- `ports.py` 只放 Protocol。Store、HTTP、解析、模型适配、页面和 CLI 由各自工作包实现。

## 冻结文件

WP0 拥有并在接纳后冻结：

- `AGENTS.md`、`.gitignore`、`pyproject.toml`
- `config/*.example.*`
- `src/second_brain/` 下的 `__init__.py`、`models.py`、`errors.py`、`ports.py`，以及各包的空 `__init__.py`
- `tests/conftest.py`、`tests/test_models.py`、`tests/test_boundaries.py`

后包不要改这些文件，也不要另造一套字段、哈希或异常。缺口回到 WP0 一次修改。

`README.md`、`cli.py`、`config.py` 属于 WP14。WP0 不建它们的占位实现。

## 检查

```bash
PYTHONPATH=src python3 -m pytest tests/test_models.py tests/test_boundaries.py -q
```
