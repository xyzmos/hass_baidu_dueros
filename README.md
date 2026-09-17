<p align="center">
  <img src="custom_components/hass_baidu_dueros/brand/icon.png" width="96" alt="HASS Baidu DuerOS">
</p>

# HASS Baidu DuerOS（小度音箱）

Home Assistant 自定义集成：通过小度开放平台「智能家居 → HTTP 自建技能」，把 HA 中的实体接入小度音箱，实现语音控制、状态查询与状态主动上报。

[![通过 HACS 添加仓库](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=xyzmos&repository=hass_baidu_dueros&category=integration)

仓库地址：
- GitHub：<https://github.com/xyzmos/hass_baidu_dueros>

## 特性

- **全流程自建**：OAuth2 授权页、Token 签发、技能服务端点均由集成自身提供，不依赖第三方中转服务；
- **复用 HA 暴露机制**：暴露范围与官方「语音助手」（Assist）设置完全一致，无需维护额外白名单；
- **多域支持**：`climate`、`cover`、`fan`、`humidifier`、`light`、`media_player`、`scene`、`switch`、`vacuum`、`sensor`；
- **房间分组**：自动识别实体区域（HA 区域 → 设备区域 → 名称前缀），以小度「设备分组」同步，可按房间语音控制；
- **状态主动上报**：HA 实体状态变化时推送 `ChangeReportRequest`，小度 APP 状态实时同步；
- **自动同步设备**：HA 重启、暴露配置变更后自动通知小度重新发现（devicesync）；
- **内置定时调度**：小度语音「X 分钟后开/关……」由集成执行，HA 重启后自动恢复未完成任务；
- **设备 ID 加密**：可选 16 位密钥对 applianceId 做 AES 加密，避免设备 ID 明文外泄；
- **日志脱敏**：access_token 仅保留前 6 位。

## 环境要求

- Home Assistant ≥ 2025.7.0；
- HA 必须能通过**公网 HTTPS 域名**访问（小度开放平台的授权地址、Token 地址、WebService 地址均要求 HTTPS）；
- 依赖 Python 包 `pycryptodome`、`PyJWT`，由 Home Assistant 自动安装。

## 安装

### 通过 HACS（推荐）

1. HACS → 右上角菜单 → 自定义仓库；
2. 填入仓库地址，类别选择「集成」：
   - `https://github.com/xyzmos/hass_baidu_dueros`
3. 搜索「小度音箱」并安装，重启 Home Assistant。

### 手动安装

1. 从上述任一仓库下载源码；
2. 将 `custom_components/hass_baidu_dueros` 整个目录放入 HA 配置目录的 `config/custom_components/` 下；
3. 重启 Home Assistant。

## 配置

### 一、小度开放平台

1. 在小度开放平台创建「智能家居」技能（HTTP 自建技能），记录技能 ID（Bot ID）、Client ID 与 Client Secret；
2. 在技能的服务配置中填写以下三个地址（`<HA 地址>` 替换为你的公网 HTTPS 域名，不要带结尾斜杠）：

| 配置项 | 地址 |
|---|---|
| 授权地址 | `https://<HA 地址>/hass_baidu_dueros/auth/authorize` |
| Token 地址 | `https://<HA 地址>/hass_baidu_dueros/auth/token` |
| WebService | `https://<HA 地址>/hass_baidu_dueros/service` |

### 二、Home Assistant

1. **设置 → 设备与服务 → 添加集成 → 小度音箱**（仅允许配置一个实例），填写：

| 字段 | 说明 |
|---|---|
| Client ID | 与小度开放平台一致，必须以 `dueros` 开头（`dueros-test` 用于小度测试平台） |
| Client Secret | 与小度开放平台一致 |
| HA 外部访问地址 | HA 公网 HTTPS 地址（如 `https://ha.example.com`），留空则自动检测 |
| 技能 ID / Bot ID | 小度开放平台的技能 ID |
| 设备 ID 加密密钥 | 选填，恰好 16 个字符；填写后 applianceId 将被 AES 加密 |

2. 在小度 APP 中重新「发现设备」，并使用 **HA 账号密码**完成授权登录。

> 授权登录页由集成提供，账号密码仅用于调用 HA 自身的登录接口。若账号启用了两步验证（MFA），页面会明确提示，请改用未启用 MFA 的账号授权。

## 设备暴露

集成不维护独立的设备清单，暴露范围完全等同 Home Assistant 的「暴露给语音助手」设置（以 Assist 助手，即 `conversation` 的开关为准，判定直接调用 HA 官方接口）：

- 在实体的 **设置 → 语音助手** 中勾选/取消「暴露」，即可控制是否同步到小度；
- 未手动设置过的实体按 HA 默认规则判定：
  - 默认暴露域：`climate`、`cover`、`fan`、`humidifier`、`light`、`media_player`、`scene`、`switch`、`vacuum`（HA 还会默认暴露 `todo`、`water_heater`，本集成不支持）；
  - `sensor` 按设备类别默认暴露：温度、湿度、PM2.5、PM10、CO₂、空气质量、挥发性有机物，其余（电量、信号强度等）需在实体设置中手动开启；
  - 诊断/配置类实体（`entity_category` 非空）与已隐藏（hidden）的实体默认不暴露；
- 即使已暴露，也只有本集成支持的域会被同步到小度：`climate`、`cover`、`fan`、`humidifier`、`light`、`media_player`、`scene`、`switch`、`vacuum`、`sensor`；
- 修改暴露设置后，集成会自动重建缓存（约 2 秒防抖）并通知小度重新发现设备。

设备名称取实体的 `friendly_name`（清除标点、限制 128 字符）；区域判定顺序：

1. 实体所属 HA 区域；
2. 实体所属设备（集成/设备）所属区域；
3. `friendly_name` 以常见房间名（客厅、卧室、书房……）开头；
4. 所在 group 名称中包含常见房间名。

## 支持的域与设备类型

| HA 域 | 小度设备类型 | 主要能力 |
|---|---|---|
| `light` | LIGHT | 开关、亮度、颜色、色温 |
| `climate` | AIR_CONDITION | 开关、温度、模式、风速 |
| `switch` | SWITCH | 开关、定时 |
| `sensor` | SENSOR | 温度、湿度、PM2.5、PM10、CO₂、甲醛、光照、空气质量查询 |
| `cover` | CURTAIN | 打开、关闭、暂停 |
| `fan` | FAN / YUBA（名称含「浴霸」） | 开关、风速、摆风 / 档位、模式 |
| `humidifier` | HUMIDIFIER | 开关、目标湿度查询与设置 |
| `media_player` | TV_SET | 开关、播放 / 暂停、音量调节与静音 |
| `vacuum` | SWEEPING_ROBOT | 启动、回充、暂停、模式、吸力、水量、电量与状态查询 |
| `scene` | SCENE_TRIGGER | 场景激活 |

> 指令会按实体实际能力裁剪：例如灯不支持色温时不会暴露色温指令，`media_player` 不支持音量调节时不会暴露音量指令。

灯的色温指令使用 `light.turn_on` 的 `color_temp_kelvin` 参数下发，色温值按设备实际范围（`min_color_temp_kelvin` / `max_color_temp_kelvin`）钳制，避免超出灯带支持区间导致调用失败。

## 提供的服务

| 服务 | 说明 |
|---|---|
| `hass_baidu_dueros.reload` | 重新加载设备配置并通知小度平台重新发现设备 |
| `hass_baidu_dueros.debug_discovery` | 调试用，触发一次设备发现并把结果输出到日志 |

## 常见问题

**小度 APP 中看不到设备？**

1. 确认实体在「语音助手」（Assist）中处于暴露状态，且实体所属域在集成支持范围内；
2. 调用 `hass_baidu_dueros.reload` 服务或重启 HA；
3. 在小度 APP 中重新「发现设备」；
4. 查看日志中的 `[init] loaded N exposed devices` 与发现结果。

**运行一段时间后提示「设备绑定账号已失效，请重新绑定」？**

该提示来自小度，成因是它刷新 access token 失败。集成已做两处防护：

1. 刷新时沿用签发 refresh_token 时登记的 `client_id`（HA 要求两者完全一致，否则返回 `invalid_request`），映射持久化在 `.storage/hass_baidu_dueros_client_ids`，HA 重启后仍生效；
2. 上报被拒时的日志会带上 HA 的 `error` 与 `error_description`，可直接区分 `client_id` 不一致、`client_id` 非法与 token 失效。

若日志中出现 `token exchange failed ... error=invalid_grant`，说明 refresh_token 已不存在（例如小度侧解绑、HA 侧令牌被删除），此时按提示重新绑定即可。

**授权时提示「该 HA 账号启用了两步验证（MFA）」？**

集成通过 HA 登录接口校验账号密码，暂不支持 MFA 二次验证，请改用未启用 MFA 的账号授权。

**语音控制提示设备不在线？**

实体状态为 `unavailable` / `unknown` 时会标记为不可达（`isReachable: false`），请先在 HA 中确认实体可用。

**小度 APP 状态不同步？**

主动上报依赖首次授权后缓存的 `openUid`，请先在小度 APP 完成一次绑定与设备发现。若仍不生效，可在日志中检索 `ChangeReport` 的返回结果排查。

日志出现 `status: 21207`（`One attribute can only sync 1 times during 60`）说明小度对同一属性有 60 秒一次的同步限制；集成会自动把窗口内的多次变更合并为一次延迟补发，属正常限流，无需处理。

日志出现 `status: 21096`（`Cloud response name is not ReportStateResponse`）表示小度回查设备属性时未得到 `ReportStateResponse`。集成现已保证该响应名恒定（即使属性读取失败也不替换为错误名），并会在 applianceId 失效时自动触发一次设备同步；若仍反复出现，请在小度 APP 重新发现设备。

**修改「设备 ID 加密密钥」后设备全部失效？**

applianceId 会随密钥变化，需在小度 APP 中重新发现设备并重新绑定。建议首次配置完成后不要修改该密钥。

**暴露实体过多导致发现失败？**

单次设备发现上限为 300 个，超出部分会被截断。建议仅暴露必要实体。

## 调试

```yaml
logger:
  logs:
    custom_components.hass_baidu_dueros: debug
```

日志中所有 `access_token` / `token` 均会自动脱敏（仅保留前 6 位）。

## 实现说明

| 端点 | 作用 |
|---|---|
| `POST /hass_baidu_dueros/service` | 技能服务入口，校验 HA access token 后分发到对应平台处理器 |
| `GET/POST /hass_baidu_dueros/auth/authorize` | OAuth2 授权页，校验 HA 账号密码后跳转回小度（同 IP 60 秒内最多 5 次失败尝试） |
| `POST /hass_baidu_dueros/auth/token` | Token 端点，转发至 HA `/auth/token`，并将 access token 有效期延长至 24 小时 |

- 设备 ID 默认格式为 `hbd_<domain>_<object_id>`，配置加密密钥后为 AES 加密串；
- 设备状态上报 `ChangeReportRequest` 与设备同步 `devicesync` 直连小度开放平台接口；
- 定时任务以 `(entity_id, 开/关方向)` 为键持久化，HA 重启后自动恢复，同方向重复设定以最后一次为准；
- 错误消息统一使用 `DuerOS.ConnectedHome.Control` 命名空间（协议规定），参数类错误返回 `UnexpectedInformationReceivedError` 并携带 `faultingParameter`；
- 属性上报按「设备属性」白名单转换（传感器不上报 `turnOnState`，风扇 `oscillating` 上报为 `SWING`/`STOP`），并按 60 秒窗口做同属性合流；
- `ReportStateResponse` 会返回小度请求的属性（含 `location`、`electricityCapacity` 等非实体属性）；
- 单次发现：设备上限 300、分组上限 10、每组设备上限 50、每设备属性上限 10。

## 许可证

[Apache License 2.0](LICENSE)
