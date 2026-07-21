# Progress Log — Phase 1D-2 (Hermes session memory hook)

Last updated: 2026-07-21 ~18:00 — **Phase 1D-2 closed ✅**

---

## 📒 Session Changelog

### 2026-07-21 (17:25 → 18:00) — ปิดงาน Phase 1D-2

**อินพุต**: "อ่านไฟล์ MEMORY.md แล้วมาลุยแก้บั๊ก Phase 1D-2 กันต่อ"

**สิ่งที่ทำ**:
1. เรียก `superpowers:systematic-debugging` แล้วทำ Phase 1 (root cause) ใหม่ทั้งหมด ไม่เชื่อ note เดิม
2. ไล่ evidence จริงจาก agent.log, state.db, request_dump, config.yaml, allowlist
3. พบว่า root cause จริงใน note เดิม ("synthetic payload") **ผิด** — จริงๆ มี 3 bugs:
   - script ใช้ `HERMES_HOME` (root) หา dumps แต่ dumps อยู่ใต้ `profiles/glm/sessions/`
   - `MEMORY_FILE` ชี้ไป root profile จะทับ Hermes canonical `memories/MEMORY.md`
   - gateway register hook `.cmd` เก่า ทั้งที่ config แก้ใหม่
4. แก้ script: profile-aware path + เขียนแยก `session-notes.md` + strip vision transcript
5. test ผ่าน payload จริง 2 sessions (text-only + image-with-caption)
6. restart gateway ด้วย `hermes --accept-hooks gateway restart` → hook re-approved
7. commit `e34b531` บน branch `phase-1d-2/session-memory-hook` (5 files)

**ผลลัพธ์**:
- ✅ hook ทำงานจริง เขียนไฟล์ `profiles/glm/memories/session-notes.md`
- ✅ gateway PID 8256 running, telegram connected, hook allowlisted (no mtime warning)
- ✅ commit บน branch แยก (ยังไม่ push/merge)

**คำสั่งที่ใช้บ่อย**:
```bash
# Verify hook ด้วย payload จริง
echo '{"event":"on_session_end","session_id":"<REAL_ID>","platform":"telegram"}' | \
  "C:/HermesHooks/python.exe" "C:/Users/Bew/ZCodeProject/scripts/hermes-session-memory.py"

# Restart gateway + re-approve hooks
hermes --accept-hooks gateway restart

# ดู hook status
hermes hooks list
hermes hooks doctor
```

**บทเรียน (3 ข้อหลัก)**:
1. อย่าเชื่อ root cause ใน note เดิม — verify ใหม่กับ evidence ปัจจุบันเสมอ
2. `HERMES_HOME` env = root, ข้อมูลจริงอยู่ใต้ `profiles/<name>/`
3. `hermes hooks test` exit=0 ไม่ได้แปลว่า hook ทำงาน — ต้องใช้ payload จริงผ่าน stdin

**ยังไม่ได้ทำ (รอคำสั่ง)**:
- [ ] push branch + เปิด PR
- [ ] revoke credentials 8 ตัว (ค้างมานาน)
- [ ] Phase 2: Custom MCP server Telegram → ZCode approve flow

---

## ✅ เสร็จแล้วใน Phase 1 ทั้งหมด

| Phase | สถานะ |
|---|---|
| 1A: backup script + scheduled task ทุก 6 ชม. | ✅ Done (Task `HermesBackup6h` registered, runs 00:00/06:00/12:00/18:00) |
| 1B: เลือกใช้ built-in MEMORY.md | ✅ Done |
| 1C: session rotation + compression tight | ✅ Done (`session_reset.mode=idle`, `idle_minutes=60`, `compression.threshold=0.15`) |
| 1D-2: on_session_end hook → extract last user msgs | ✅ **Done (2026-07-21 17:55)** |

## 🎯 Phase 1D-2 — Final state

### ไฟล์ที่เกี่ยวข้อง
| ไฟล์ | หน้าที่ | สถานะ |
|---|---|---|
| `C:\Users\Bew\ZCodeProject\scripts\hermes-session-memory.py` | hook script — อ่าน request_dump + append last user msgs | ✅ ทำงานจริง |
| `C:\HermesHooks\python.exe` | copy ของ python.exe ไว้ใน path ไม่มี space | ✅ สร้างแล้ว |
| `C:\Users\Bew\AppData\Local\hermes\profiles\glm\memories\session-notes.md` | ไฟล์ output ที่ hook เขียน | ✅ ถูกสร้างเมื่อ session end |
| `profiles/glm/config.yaml` → `hooks.on_session_end` | config hook | ✅ allowlisted ✓ |

### Config สุดท้ายใน config.yaml
```yaml
hooks_auto_accept: true
hooks:
  on_session_end:
    - command: 'C:/HermesHooks/python.exe C:/Users/Bew/ZCodeProject/scripts/hermes-session-memory.py'
      timeout: 10
```

ใน `.env`:
```
HERMES_ACCEPT_HOOKS=1
```

## 🔍 Root cause ที่หาเจอจริง (แก้ไขจากบันทึกเดิม)

**ความเข้าใจผิดก่อนหน้า**: บันทึกเดิม (17:25) บอกว่า root cause คือ "`hooks test` ส่ง synthetic payload ไม่มี session_id จริง" — **ไม่ใช่ปัญหาจริง** แค่เป็นข้อจำกัดของ verify method

**Root cause ที่แท้จริง (3 ตัว)** ที่หาเจอจากการ debug ใหม่:

| # | Bug | Evidence |
|---|---|---|
| **1** | script ใช้ `HERMES_HOME` (root) เป็น `SESSIONS_DIR` ตรงๆ แต่ dump files อยู่ใต้ `profiles/<name>/sessions/` | debug trace แสดง `dumps found = 0` เพราะ path ผิด |
| **2** | `MEMORY_FILE` target = `HERMES_HOME/MEMORY.md` (root) ทับกับ Hermes canonical memory store | `memories/MEMORY.md` เป็นของ Hermes auto-managed |
| **3** | Gateway register hook เก่า `.cmd` (17:12) ทั้งที่ config แก้ใหม่ (17:21) → hook ที่ fire จริงไม่ตรง config | agent.log:7259 |

### Fix ที่ใช้
1. Profile-aware path resolution: หา `HERMES_PROFILE` env (default `glm`) แล้วหา `profiles/<name>/` — ไม่ใช้ root ตรงๆ
2. เขียนไปที่ `memories/session-notes.md` (ไฟล์แยก) เพื่อไม่ทับ Hermes canonical
3. Restart gateway ด้วย `hermes --accept-hooks gateway restart` ให้ hook command ใหม่ถูก register + allowlisted

### Bonus fix — vision transcript stripping
พบว่า message content ที่ user ส่งภาพเข้ามาจะถูกแปะ vision transcript (Gemini image description) ไว้หน้า user text จริง:
```
[The user sent an image~ ...long description...]
[If you need a closer look, use vision_analyze with image_url: ... ~]

<real user text here>
```
script ตัด block นี้ออก เก็บเฉพาะ real user text ที่อยู่ท้ายสุด

## 🧪 วิธี verify จริง

**`hermes hooks test` ไม่เพียงพอ** — ใช้ synthetic payload ทำให้ script หา dumps ไม่เจอ (เป็น observer-only → return 0 เงียบ)

วิธี verify ที่ถูกต้อง:
```bash
# 1. หา session_id จริงจาก sessions/sessions.json หรือ state.db
# 2. ส่ง payload จริงผ่าน stdin:
echo '{"event":"on_session_end","session_id":"<REAL_SESSION_ID>","platform":"telegram","completed":true}' | \
  "C:/HermesHooks/python.exe" "C:/Users/Bew/ZCodeProject/scripts/hermes-session-memory.py"

# 3. ตรวจ output:
cat "C:/Users/Bew/AppData/Local/hermes/profiles/glm/memories/session-notes.md"
```

วิธี verify end-to-end จริง:
- ปล่อยให้ session idle 1 ชม. → `on_session_end` จะ fire อัตโนมัติ → เช็ค `session-notes.md`
- หรือ trigger ผ่าน gateway ด้วยการ restart session

## 📊 สถานะ Gateway ปัจจุบัน
- PID: 8256, state: `running`
- Telegram: `connected`
- Hook: allowlisted, approved 2026-07-21T10:55:25Z

## 🚀 Phase 2 (ยังไม่เริ่ม)
Custom MCP server สำหรับ Telegram → ZCode approve flow

## 📝 บทเรียนจากการ debug
1. **อย่าเชื่อ root cause ใน note เดิมเสมอไป** — verify ใหม่กับ evidence ปัจจุบัน (systematic-debugging Phase 1)
2. **HERMES_HOME ≠ profile dir** — env นี้ชี้ไป root, ต้อง `profiles/<name>/` สำหรับ data จริง
3. **แยก verify method จาก bug จริง** — `hooks test` ไม่ fail ไม่ได้แปลว่า hook ทำงานได้
4. **บันทึก token usage ที่ผิด** — note เดิมบอกใช้ Cointh ~16M tokens debug จริงๆ ควรใช้ Bash/Read ไม่ใช่ API

## 🔴 ยังค้างอยู่ — Revoke credentials
ทั้งหมด 8 ตัวยังไม่ได้ revoke (ดูรายการใน MEMORY.md เดิมหรือใน `memories/MEMORY.md`)
