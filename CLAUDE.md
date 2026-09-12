# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ภาพรวม

บอทกดปุ่มในโปรแกรม **ProMaxx Report** (`promaxxreport.exe` — PowerBuilder 12.5.2) เพื่อ
export รายงาน R05.106 (บาร์โค้ดสินค้า) เป็น CSV แล้วอัปโหลดเข้า Supabase อัตโนมัติทุกวัน

สองภาษาแบ่งหน้าที่ชัดเจน — **อย่ารวมกัน**

| ส่วน | ภาษา | ไฟล์ |
|---|---|---|
| ขับ GUI ของ ProMaxx | Python 3.11 | `run.py`, `bot/` |
| อัปโหลดเข้า Supabase | Node.js (ESM) | `upload-products.mjs` |

`bot/upload.py` เป็นแค่ตัวเรียก `node upload-products.mjs` แล้วส่ง exit code กลับ —
ไม่มีตรรกะอัปโหลดใน Python เลย เหตุผลอยู่ใน docstring ของไฟล์นั้น (สรุป: `.mjs`
มีเทส 11 ตัวที่เคยจับบั๊ก UTF-8 BOM / quoted newline / DELETE ไร้ WHERE มาแล้ว
เขียนใหม่เป็น Python = เปิดช่องให้บั๊กชุดเดิมกลับมา)

## คำสั่งที่ใช้บ่อย

```powershell
# รัน flow (ทุกคำสั่งใช้ .exe แทน "python run.py" ได้ ถ้า build แล้ว)
python run.py run flows/r14_102_export.yaml                    # สายเต็ม: ล็อกอิน -> ส่งออก CSV
python run.py run flows/login.yaml --dry-run                   # หา control แต่ไม่กดจริง

# สำรวจหน้าจอ (ใช้ตอนสร้าง flow ใหม่)
python run.py inspect --launch          # dump ครั้งเดียว
python run.py inspect --watch           # dump ทุกครั้งที่หน้าจอเปลี่ยน แล้วคลิกมือทีละขั้น
python run.py actions                   # ดู action ที่ใช้ใน YAML ได้ทั้ง 20 ตัว
python run.py stop

# เทส (มีเฉพาะฝั่ง Node — ฝั่ง Python ยังไม่มีชุดเทส)
npm test                                          # 11 ตัว
node --test upload-products.test.mjs              # เหมือนกัน
node --test --test-name-pattern "isStaleFile" upload-products.test.mjs   # ตัวเดียว

# ทดสอบ uploader โดยไม่แตะ Supabase
node upload-products.mjs --dry-run --file "path\to\R05.106.CSV"

# build เป็น .exe (~34 MB, onedir)
.\tools\build_exe.ps1

# ตั้งรันอัตโนมัติ
.\tools\register_task.ps1 -Time 08:30 -Flows "flows/r14_102_export.yaml"
```

## สถาปัตยกรรม

### เส้นทางการทำงานเต็ม

```
run.py (CLI)
  └─ bot/runner.py     อ่าน flow YAML → วน execute ทีละ step
       └─ bot/actions.py    REGISTRY: ชื่อ action → ฟังก์ชัน
            ├─ bot/locators.py    หา hwnd จากสเปกใน YAML
            ├─ bot/win.py         ยิง window message (ชั้นล่างสุด)
            ├─ bot/datawindow.py  จัดการ DataWindow ของ PowerBuilder
            └─ bot/watch.py       เทียบภาพก่อน-หลังว่าการกดมีผลจริง
  └─ bot/upload.py     (เมื่อใส่ --then-upload) เรียก node upload-products.mjs
                       ** ยังเป็นของ R05.106 เท่านั้น ดูหัวข้อถัดไป **
```

**flow ซ้อนกันเป็นชั้น** ผ่าน action `run_flow` — ตัวนอกสุดคือ `r14_102_export.yaml`
ซึ่งเรียกลึกลงไปถึง `login.yaml` แต่ละชั้นเพิ่มทีละขั้นตอน แก้ชั้นล่างแล้วกระทบทุกชั้นบน
ซ้อนได้ไม่เกิน `MAX_FLOW_DEPTH = 12` ใน `bot/runner.py` (เดิม 5 ไม่พอสายนี้)

```
login → r14_1 → r14_102 → r14_102_generate → r14_102_warehouse
      → r14_102_warehouse_select → r14_102_year → r14_102_confirm → r14_102_export
```

**สายนี้ย้ายมาจาก R05.106 ทั้งเส้น** ไฟล์ `r05_*.yaml` ถูกเปลี่ยนชื่อหมดแล้ว ไม่เหลือในโปรเจกต์
เงื่อนไขที่ตั้งให้รายงาน: คลัง อนิน สาขาแยกชากค้อ (Warehouse, Front Store) / เก้ากิโล /
อนิน สาขาสวนเสือศรีราชา · ประจำปี-ช่วงปี 2023-2026 · สินค้าทั้งหมด
ได้ไฟล์ `ADJ_R14.102.CSV` ราว 46 MB (130,408 แถว 27 คอลัมน์) ใช้เวลาทั้งสายราว 75 วินาที

**ส่วนอัปโหลดยังเป็นของ R05.106** `upload-products.mjs`, `bot/upload.py`,
`tools/run_and_upload.ps1` ยังยึดหัวคอลัมน์ของ R05.106 (ตาราง products)
`app.default_then_upload` จึงตั้งเป็น false และ**ห้ามรันสายนี้ด้วย `--then-upload`**
จนกว่าจะเขียนตัวอัปโหลดของ R14.102 (ถ้าเผลอสั่ง uploader จะหยุดเองเพราะหาคอลัมน์
CF_BARCODE / CF_FMLPRICE / CF_BASEMULTIPLE ไม่เจอ ตาราง products ไม่ถูกแตะ)

flow ระหว่างทางตั้งใจ**ไม่ปิดโปรแกรม** เพื่อให้ `inspect` ส่องหน้าจอต่อได้
มีแต่ `r14_102_export.yaml` ที่มี `stop_app` ปิดท้าย

### เพิ่ม action ใหม่

ลงทะเบียนด้วย decorator ใน `bot/actions.py` — **ต้องประกาศคีย์ที่รับใน tuple ด้วย**
ไม่งั้น `validate_flow` จะตีว่าเป็นคีย์ผิดแล้ว flow ล้มตั้งแต่ยังไม่รัน

```python
@action("move_file", ("from", "to", "overwrite"))
def act_move_file(ctx: Context, step: dict) -> None:
    ...
```

`COMMON_KEYS` (`name`, `retry`, `optional`, `timeout`, `pause`) ถูกเติมให้อัตโนมัติทุก action

### การหา path เมื่อ build เป็น .exe

`bot/config.py` มี `ROOT` ตัวเดียวที่ทุกอย่างอ้างอิง (`settings.yaml`, `.env`, `flows/`,
`logs/`, `screenshots/`) — เช็ค `sys.frozen` แล้วใช้โฟลเดอร์ของ `sys.executable`
เพราะตอน frozen `__file__` ชี้เข้า bundle ชั่วคราว ไม่ใช่ที่ที่ผู้ใช้วางไฟล์จริง

**เพิ่ม path ใหม่ต้องผ่าน `ROOT` เสมอ** ห้าม hardcode relative path จาก cwd

## ข้อจำกัดของโปรแกรมเป้าหมายที่กำหนดวิธีเขียนโค้ด

### ทุกอย่างต้องเป็น window message

รันตอนจอล็อกได้เพราะไม่มีการขยับเมาส์/คีย์บอร์ดจริงเลย — **ห้ามใช้ `pyautogui`,
`click_input()`, `type_keys()`, `ImageGrab`** ในโค้ดนี้

| งาน | ใช้ |
|---|---|
| กดปุ่ม | `WM_LBUTTONDOWN/UP` หรือ `BM_CLICK` |
| พิมพ์ | `WM_CHAR` ทีละตัว |
| อ่านข้อความ | `WM_GETTEXT` (`GetWindowText` อ่านข้าม process ไม่ได้) |
| ถ่ายภาพ | `PrintWindow` (`ImageGrab` ได้ภาพดำเมื่อจอล็อก) |

`pywinauto` อยู่ใน `requirements.txt` แต่**ไม่ได้ถูก import ที่ไหนเลย** เพราะช่วยอะไร
กับ DataWindow ไม่ได้

### DataWindow วาดเอง ไม่มี control จริง

ช่องกรอกและปุ่มใน `pbdw125` ไม่ใช่ control จริง หา handle รายช่องไม่ได้ ต้อง
คลิกพิกัด → PowerBuilder เลื่อน in-place editor (`Edit` ที่ซ่อนอยู่) มาทับ → พิมพ์ลงตัวนั้น

**ต้องใช้ `method: chars` ไม่ใช่ `settext`** — `WM_SETTEXT` ไม่แจ้ง DataWindow ว่าค่าเปลี่ยน
PowerBuilder จึงไม่ commit เข้า column buffer

### ตรวจว่าการกดมีผลจริง — เรียงจากดีที่สุด

PowerBuilder เงียบสนิทเมื่อกดพลาด ทุกการกดสำคัญต้องมีตัวยืนยัน

1. `assert_text` — อ่านข้อความจากโปรแกรมตรง ๆ แน่นอนที่สุด
2. `wait_window` — เมื่อการกดทำให้มีหน้าต่างใหม่โผล่
3. `expect_change` — เทียบภาพ ใช้เมื่อไม่มีอะไรให้ยึดนอกจาก pixel

รายละเอียดกับดักของ `expect_change` (แถบไฮไลต์แถว, caret กะพริบ, ภาพยังไม่นิ่ง,
พื้นที่รายงานถูกล้างเป็นสีขาวก่อนดึงข้อมูล) อยู่ใน README หัวข้อ "กับดักที่เจอมาแล้ว 4 อย่าง"

**รายงานใหญ่ห้ามยืนยันด้วยการเทียบภาพพื้นที่รายงาน** — ผ่านตั้งแต่วินาทีแรกที่หน้าถูกล้าง
ทั้งที่รายงานยังไม่ออก ให้รอ `Edit` id 1004 (แถบสรุปเงื่อนไขใต้รายงาน) โผล่ด้วย
`wait_control` แล้ว `assert_text` ข้อความเงื่อนไข (ดู `flows/r14_102_confirm.yaml`)
ระหว่างสร้างรายงาน โปรแกรมหยุดตอบ message ทั้งตัว — `WM_GETTEXT` คืนค่าว่าง
แต่ `wait_control` ที่หาด้วยคลาสกับ id อย่างเดียวยังทำงานได้

## ความปลอดภัยของข้อมูลปลายทาง

### เผยแพร่ไฟล์แบบ temp แล้วเปลี่ยนชื่อ

`r14_102_export.yaml` เขียนลง `.part.CSV` → `assert_file` ตรวจ → `move_file` เปลี่ยนชื่อ
ถ้า flow ล้มกลางทาง ไฟล์ของรอบก่อนยังอยู่ครบ และปลายทางไม่มีวันหยิบไฟล์ที่เขียนไม่เสร็จ

`assert_file` มี `max_age` กันไฟล์เก่าจากรอบก่อนถูกนับว่าผ่าน

### uploader หยุดตัวเองเมื่อข้อมูลผิดปกติ

`upload-products.mjs` ตรวจก่อนเขียนทุกรอบ

| เงื่อนไข | ผล |
|---|---|
| ไฟล์ไม่ได้แก้วันนี้ | ข้าม (exit 2) — ไม่ใช่ error |
| ไฟล์เพิ่งถูกแก้ < 1 นาที | รอจนนิ่งก่อน (อาจ export ยังไม่เสร็จ) |
| แถวน้อยกว่าเดิมเกิน 20% | หยุด ไม่แตะข้อมูลเดิม (ต้อง `--force` ถึงจะผ่าน) |

**exit code มีความหมาย**: `0` สำเร็จ · `1` ผิดพลาด · `2` ข้ามรอบ (ไม่ใช่ error)
ใช้ตรงกันทั้ง `upload-products.mjs`, `bot/upload.py`, `run.py`, `tools/run_and_upload.ps1`

### เขียน Supabase ผ่านตารางพัก

ไม่ delete-all → insert ตรง ๆ เพราะพังกลางทางแล้วตาราง `products` จะว่างทั้งที่ไม่มีคนเฝ้า
วิธีที่ใช้: เขียนลง `products_import` ให้ครบก่อน แล้วเรียก RPC `swap_products_from_import()`
ซึ่งทำทั้งหมดใน transaction เดียว (ดู `products-import-swap.sql` — ต้องรันใน Supabase
SQL Editor ครั้งเดียวก่อนใช้งาน)

## ผลลัพธ์การรัน

| ไฟล์ | ใช้ดูอะไร |
|---|---|
| `logs/last_run.json` | สรุปรอบล่าสุด — `status`, `exit_code`, `outputs` (ไฟล์ที่ผลิตได้) |
| `logs/bot.log` | log เต็ม หมุนเวียนอัตโนมัติ |
| `screenshots/` | ภาพทุก step รวมภาพตอนพัง (`*_ERROR_*`) |
| `logs/inspect/` | control tree + JSON + ภาพ จากคำสั่ง `inspect` |
| `upload-products.log` | log ของ uploader แยกต่างหาก |

`outputs` ใน `last_run.json` เก็บเฉพาะไฟล์ที่**เผยแพร่จริง** — ไฟล์ `.part` ที่ถูก
เปลี่ยนชื่อไปแล้วถูกถอดออก สคริปต์ปลายทางควรเช็ค `status == "ok"` ก่อนใช้

## กับดักเฉพาะเครื่องนี้

**ไฟล์ `.ps1` ที่มีภาษาไทยต้องมี UTF-8 BOM** — PowerShell 5.1 อ่านไฟล์ไม่มี BOM ด้วย
system codepage ทำให้คอมเมนต์ไทยทำลาย syntax (วงเล็บหาย) เครื่องมือเขียนไฟล์ส่วนใหญ่
ไม่ใส่ BOM ให้ ต้องแปลงเอง

```powershell
$c = [System.IO.File]::ReadAllText($p, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText($p, $c, [System.Text.UTF8Encoding]::new($true))
```

**ไฟล์ `.bat` ตรงข้าม** — ใส่ BOM ไม่ช่วย `cmd.exe` ตีความ encoding ไม่แน่นอน
ถ้าจำเป็นต้องเขียน ให้ใช้ภาษาอังกฤษล้วน

**เครื่องนี้ตั้ง `NoDefaultCurrentDirectoryInExePath=1`** — `cmd.exe` ไม่ยอมเรียก `.exe`
ที่อยู่ใน current directory แม้ `cd` เข้าไปแล้ว (`dir` เห็นไฟล์แต่ error 9009)
ต้องเรียกด้วย path เต็มเสมอ

**VS Code file watcher ล็อกโฟลเดอร์ `dist/`** ทำให้ `Remove-Item` ทั้งโฟลเดอร์ล้มด้วย
WinError 32 — `build_exe.ps1` จึงลบไฟล์ข้างในแทนการลบตัวโฟลเดอร์

**อย่ารัน `.exe` ที่มี `pause` ค้างไว้ระหว่างทดสอบ** จะล็อก `dist/` จน build ไม่ผ่าน

## สิ่งที่เปลี่ยนได้โดยไม่ต้อง build .exe ใหม่

`flows/`, `settings.yaml`, `.env` ถูกคัดลอกไปวาง**ข้าง** exe ไม่ได้ฝังเข้าไปข้างใน
build ใหม่เฉพาะตอนแก้โค้ด Python

ค่าที่มักปรับใน `settings.yaml`: `app.default_flow` (flow ที่รันเมื่อดับเบิลคลิก),
`app.default_then_upload`, `upload.node_exe`, `app.close_timeout`

## ตัวแปรใน flow YAML

`${VAR}` ขยายจาก environment (`.env` + ตัวแปรระบบ) แบบ strict — ไม่มีตัวแปรจริง = flow ล้ม

| ตัวแปร | ค่า |
|---|---|
| `${PROMAXX_USER_CODE}` / `${PROMAXX_PASSWORD}` | จาก `.env` (gitignored) |
| `${USERPROFILE}` | ใช้แทนการ hardcode ชื่อผู้ใช้ในพาธ |
| `${BOT_DATE}` / `${BOT_TIME}` / `${BOT_DATETIME}` | เติมให้เองใน `bot/config.py` |
