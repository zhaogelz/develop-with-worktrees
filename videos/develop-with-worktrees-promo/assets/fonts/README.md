# 中文字体资产

`NotoSerifSC-400.ttf` 是按本片全部可见文字生成的 Noto Serif SC 400 子集，用于在无系统中文字体的环境中稳定导出。

- 来源：[Google Fonts 的 Noto Serif SC CSS API](https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400&display=swap)。通过 `text` 参数请求新版画面的全部 227 个字符，并核对返回 TTF 的 Unicode 字形映射，确认逐字覆盖。
- 许可证：SIL Open Font License 1.1，全文见 [LICENSE-OFL-1.1.txt](LICENSE-OFL-1.1.txt)。
- 上游项目：[Noto CJK](https://github.com/notofonts/noto-cjk)。
- [font-subset.json](font-subset.json) 保存已核验字符与字体 SHA-256；仓库检查入口先比对实际画面文字和此清单，再运行浏览器检查。

修改画面文字后，运行仓库根目录下的命令：

```text
uv run python videos/develop-with-worktrees-promo/scripts/check.py
```

若提示新增字符缺少字形，重新向同一官方 API 请求完整文字子集，核对字体内的字符映射后，同时更新 TTF 和覆盖清单。仅修改清单不能补全字体。
