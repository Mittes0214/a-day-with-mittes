# 服饰图片与画风

生成、重画或编辑服饰设定图前，必须先读 `art-direction/STYLE.md`，并查看本次对应的固定参考图。用户明确要求换画风时，以用户指令为准。

- 固定参考：`art-direction/reference-outer.png` 与 `reference-inner.png`。不得把最新生成图自动升级为画风基准。
- 使用 `art-direction/build_prompts.py` 从现行 `wardrobe.toml` 生成提示词；脚本会校验参考图摘要。新图必须传入对应参考图片，不能只写“二次元风格”。
- 外搭、内搭分别出图；无人、无人台、不画身体，内搭不加安全裤。这里只规定服饰资料图的表现方式。
- 保存完整提示词、参考图角色、衣柜文字快照和最终图片；新版本不覆盖旧资产。外搭更新 `outfit_images.toml`，内搭更新 `innerwear_images.toml`，两份必须与衣柜套装名称一致。
- 衣柜文字不进入 story prompt。修改展示时不要顺带改变 planner / replyer 的参考资料协议。
