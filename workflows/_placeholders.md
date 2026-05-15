# ComfyUI Workflow 节点占位符约定

> M1 强制约定：**代码不写死任何节点 ID**。所有需要"注入"的节点 ID 全部从 `config/node_mapping.yaml` 读取。

## 为什么

ComfyUI workflow JSON 中每个节点的数字 ID（如 `"6"`, `"42"`) 在不同导出/不同版本中会变。Sulphur 2 仓库自带的 `workflows/ltx23_t2v distilled.json` 在你导入并保存一次后，节点 ID 也可能与原文件不同。

代码层面只认"**角色**"（如 "正向提示词节点"），由配置把"角色 → 节点 ID"映射起来，workflow 升级时只改配置，不改代码。

---

## M1 阶段需要标注的角色（5 个）

| 角色 (key) | 用途 | 在 ComfyUI 里通常显示为 |
|---|---|---|
| `positive_prompt_node` | 注入正向提示词 | `CLIPTextEncode` 节点（标 "Positive"） |
| `negative_prompt_node` | 注入负向提示词 | `CLIPTextEncode` 节点（标 "Negative"） |
| `sampler_node` | 注入 seed | `KSampler` / `LTXSampler` / `SamplerCustom` 等 |
| `empty_latent_node` | 注入 width / height / num_frames | `EmptyLTXLatentVideo` / `EmptyLatentVideo` |
| `save_video_node` | 读取输出文件名 | `SaveVideo` / `VHS_VideoCombine` |

---

## 怎么找出每个节点的 ID

1. 启动 ComfyUI Web UI，加载 `workflows/sulphur2_t2v.json`
2. 在 ComfyUI 设置里打开 **Settings → Show node IDs**（或在 Manager 中开启）
3. 每个节点左上角会显示数字 ID
4. 把 5 个 ID 填入下面的 `config/node_mapping.yaml`

---

## `config/node_mapping.yaml` 模板（放在仓库 `config/` 目录）

```yaml
sulphur2_t2v:
  positive_prompt_node: ""      # 例如 "6"
  negative_prompt_node: ""      # 例如 "7"
  sampler_node: ""              # 例如 "12"
  empty_latent_node: ""         # 例如 "9"
  save_video_node: ""           # 例如 "20"

# I2V workflow（M2 阶段再填）
sulphur2_i2v:
  positive_prompt_node: ""
  negative_prompt_node: ""
  sampler_node: ""
  load_image_node: ""           # 注入首帧图片路径
  save_video_node: ""
```

---

## 自检命令（M1 必跑）

```bash
python -m src.cli env
```

会校验 5 个 ID 是否都填了，并且这些 ID 是否真的存在于 workflow JSON 中。
