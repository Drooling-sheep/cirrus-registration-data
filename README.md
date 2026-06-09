# Cirrus 美国注册跟踪看板

这个项目用 FAA Releasable Aircraft Database 跟踪 Cirrus 飞机在美国的新注册情况，并生成一个单页 HTML 看板。默认看板口径使用“滞后估算新机注册”：有效 Cirrus 注册记录中，`YEAR MFR` 等于 `CERT ISSUE DATE` 年份或前一年的记录。它比总注册量更接近新机交付节奏，并吸收一部分年底生产、次年注册的延迟，但仍不等于 Cirrus 或 GAMA 官方全球交付量。

## 本地运行

```bash
python3 scripts/fetch_faa.py
python3 scripts/parse_faa.py
python3 scripts/scrape_aso_listings.py
python3 scripts/used_market.py
python3 scripts/serial_tracking.py
python3 scripts/flight_activity.py
python3 scripts/build_data.py
python3 -m http.server 8000
```

打开 `http://localhost:8000/web/index.html` 查看看板。

## FAA 数据

默认下载地址在 [scripts/fetch_faa.py](/Users/cailu/Documents/西锐注册数据/scripts/fetch_faa.py) 顶部的 `DEFAULT_FAA_DOWNLOAD_URL`，也可以用环境变量覆盖：

```bash
FAA_RELEASABLE_AIRCRAFT_URL="https://registry.faa.gov/database/ReleasableAircraft.zip" python3 scripts/fetch_faa.py
```

FAA 下载路径可能变化；如果下载失败，优先确认 FAA 官方 “Releasable Aircraft Database Download” 页面上的当前 zip 链接。字段布局以压缩包内 `ardata.pdf` 为准，解析脚本使用 `MASTER.txt` 和 `ACFTREF.txt` 的实际表头并校验必需字段。

## GAMA 数据

GAMA 年度交付数手动维护在 [data/gama_deliveries.csv](/Users/cailu/Documents/西锐注册数据/data/gama_deliveries.csv)，格式：

```csv
year,model_category,units
2024,piston,0
2024,jet,0
```

不要用占位数字冒充真实交付量。没有录入 GAMA 数据时，看板仍显示 FAA 注册趋势，并标注 GAMA 待录入。

## 数据口径

- `parse_faa.py` 从 `ACFTREF.MFR` 动态发现包含 `CIRRUS` 的机型，不写死型号列表。
- 有效注册状态码按 `ardata.pdf` 使用 `V` 和 `T`。
- `monthly_new` 是所有有效 Cirrus 注册按 `MASTER.CERT ISSUE DATE` 聚合，包含旧机再注册和过户影响。
- `monthly_estimated_new_aircraft` 是估算新机注册，要求 `YEAR MFR == CERT ISSUE DATE` 的年份。
- `monthly_estimated_new_aircraft_lagged` 是默认看板口径，允许 `YEAR MFR` 为注册年份或前一年。
- `monthly_estimated_new_by_model` 是估算新机注册按月和机型拆分。
- `monthly_estimated_new_lagged_by_model` 是默认看板口径按月和机型拆分。
- `by_model` 是当前有效注册存量，不是当月产量。
- `by_model_estimated_new_aircraft` 是历史估算新机注册的机型累计。
- `by_model_estimated_new_aircraft_lagged` 是默认看板口径的机型累计。
- `data/cirrus_aircraft_snapshot.json` 保存当前有效 Cirrus 注册快照；下一次自动更新时会与上一次提交的快照比较，生成 `snapshot_diff`。
- 旧机过户、注册滞后、州登记地址等都会影响 FAA 注册量与真实交付量之间的差异。

## 序列号产能代理

[scripts/serial_tracking.py](/Users/cailu/Documents/西锐注册数据/scripts/serial_tracking.py) 从当前有效 Cirrus 快照中读取 `serial_number`，按机型线统计美国可见最大序列号：

- `SR` 线：`SR20`、`SR22`、`SR22T`，共用一条序列空间。
- `SF50` 线：Vision Jet，独立序列空间。
- 未识别机型归入 `other`，不会混进 SR/SF50。

输出文件：

- [data/serial_tracking.json](/Users/cailu/Documents/西锐注册数据/data/serial_tracking.json)：当前序列号指标和历史曲线。
- [data/serial_history.json](/Users/cailu/Documents/西锐注册数据/data/serial_history.json)：跨运行累积的最大序列号历史，每周自动更新会提交它。

这是“美国可见序列号下限”，不是全球精确产量。海外交付不会进入 FAA，注册也可能滞后；适合看趋势和相对节奏，并与 GAMA 官方交付数交叉验证。

## 自动更新

[.github/workflows/update.yml](/Users/cailu/Documents/西锐注册数据/.github/workflows/update.yml) 每周一 06:00（北京时间）运行：

```text
fetch_faa.py -> parse_faa.py -> scrape_aso_listings.py -> used_market.py -> serial_tracking.py -> flight_activity.py -> build_data.py
```

工作流会提交更新后的 JSON/CSV，并把 `web/index.html` 与最终数据部署到 GitHub Pages。首次使用前需要在仓库 Pages 设置中启用 GitHub Actions 部署来源。

## ADS-B 飞行活动

[scripts/flight_activity.py](/Users/cailu/Documents/西锐注册数据/scripts/flight_activity.py) 从 FAA 快照里的 `MODE S CODE HEX` 生成 Cirrus 机队 hex 清单，并用可配置 ADS-B 源做当前状态抽样。默认源是 Airplanes.live，遵守其公开 API 的 1 request/sec 限速；也可以设为 `opensky` 或 `none`：

```bash
ADSB_PROVIDER=airplanes_live python3 scripts/flight_activity.py
ADSB_PROVIDER=none python3 scripts/flight_activity.py
```

输出文件：

- [data/flight_activity.json](/Users/cailu/Documents/西锐注册数据/data/flight_activity.json)
- [data/flight_activity_history.json](/Users/cailu/Documents/西锐注册数据/data/flight_activity_history.json)

口径：ADS-B 覆盖有盲区，PIA/LADD 会隐藏部分飞机；“看到在飞”是利用率代理，不是精确飞行小时或真实架次。项目另有 [adsb_sample.yml](/Users/cailu/Documents/西锐注册数据/.github/workflows/adsb_sample.yml) 每天抽样一次，不和 FAA 周更绑死。

## 二手市场

[scripts/used_market.py](/Users/cailu/Documents/西锐注册数据/scripts/used_market.py) 先实现免费、稳定的 FAA 过户代理：

- 单快照历史代理：`CERT ISSUE` 年份和 `YEAR MFR` 缺口大于等于 2 的，归为 `used_like_incl_renewals`。
- 跨快照前向检测：同一稳定主键的注册人 `NAME` 或 `CERT ISSUE DATE` 变化，计为过户/换证代理。首次运行没有可比基线，所以为 0 属正常。

挂牌库存与要价现在有两路输入：

- [data/listings_manual.csv](/Users/cailu/Documents/西锐注册数据/data/listings_manual.csv)：手工补充或修正模板。
- [scripts/scrape_aso_listings.py](/Users/cailu/Documents/西锐注册数据/scripts/scrape_aso_listings.py)：低频读取 ASO 公开 Cirrus 列表摘要页，输出 [data/listings_aso.csv](/Users/cailu/Documents/西锐注册数据/data/listings_aso.csv)。

ASO 抓取只读取列表页摘要，不登录、不抓详情页、不抓图片，默认每个请求间隔 2 秒，并在周更里每周运行一次。Controller 当前条款明确限制 scraping/data mining，Trade-A-Plane 返回挑战页，所以不启用自动抓取。不要价等于成交价；下架也只能作为成交弱代理。

输出文件：

- [data/used_market.json](/Users/cailu/Documents/西锐注册数据/data/used_market.json)
- [data/transfer_history.json](/Users/cailu/Documents/西锐注册数据/data/transfer_history.json)
- [data/listing_history.json](/Users/cailu/Documents/西锐注册数据/data/listing_history.json)
- [data/listings_aso.csv](/Users/cailu/Documents/西锐注册数据/data/listings_aso.csv)
