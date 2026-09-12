# IPS Offload Mechanism (Simple One-Page)

## TL;DR (5 Lines)

### English
1. Reassign one IPS case only when an engineer is above threshold.
2. Receiver must be same-group and within load cap (current cap is 9).
3. Recent receiver block uses 10-day window (group-scoped).
4. `close-pending` IPS cases are excluded from new recommendations.
5. History status and reminder rules prevent duplicate or unnecessary offload.

### 中文
1. 只有工程師超過門檻時才建議轉派一筆 IPS。
2. 接收者必須同群組且未超過負載上限（目前上限為 9）。
3. 近期接收者阻擋採 10 天視窗（群組內計算）。
4. `close-pending` 的 IPS 不再納入新建議。
5. 透過歷史狀態與提醒規則，避免重複或不必要轉派。

## English

### Purpose
- Balance issue load by recommending one reassignment from a high-load engineer to a lower-load engineer.

### Core Flow
1. Calculate current load for each engineer.
2. Apply pending-history adjustments first.
3. Find overloaded engineers (above threshold).
4. Pick one latest eligible IPS case.
5. Pick one eligible receiver in the same group.
6. Send reminder/recommendation email and update history.

### Key Rules
- Source must be overloaded and in WiFi/BT group.
- Case selection: pick the latest created eligible IPS case among overloaded reporters after history de-dup filtering.
- Receiver must be same-group, within receiver cap, and not currently blocked by pending constraints.
- Receiver cap rule: engineers with 10 or more current issues are excluded (cap = 9).
- Close-pending IPS cases are excluded from new recommendation.

### Fairness Rules
- Recent receiver filter uses pending/realized history only.
- Recent filter is group-scoped.
- Recent filter uses day window (default 10 days).
- Rotation reset happens after inactivity window (default 10 days).

### Why Team Should Feel It Is Fair
- Same standard for everyone: same threshold, same receiver cap, same group rules.
- No hidden preference: candidate ranking is load-first with deterministic tie-break.
- No repeat burden: recent receiver block avoids assigning to the same person repeatedly.
- No stale penalty: old history automatically loses effect after the 10-day window.
- No unnecessary transfer: close-pending and no-longer-needed cases are filtered out.

### Transparency Checklist
- Every recommendation can be explained by 3 numbers: source load, receiver load, threshold/cap.
- History keeps all outcomes: pending, realized, diverted, cancelled.
- Logs show rotation scope, recent receivers, and skip reasons.

### Pending Reminder Rule
- Send reminder only when offload is still needed now:
  - source still above threshold,
  - source still heavier than receiver,
  - receiver still within cap.

### History Status
- pending: recommended, waiting action
- realized: moved to intended receiver
- diverted: moved, but not to intended receiver
- cancelled: manually or policy-cancelled

### Email Behavior
- dry-run: generate/log only
- send mode: send reminder/info/recommendation based on current state

### Q&A
Q1. Why was I selected as receiver?
A1. You were in the same group, within cap, and among the least-loaded eligible candidates.

Q2. Why was I not selected even with low load?
A2. Common reasons: recent receiver window, pending receiver block, or group mismatch.

Q3. Why was a pending transaction cancelled?
A3. It was manually reversed or became unnecessary by policy (for example close-pending case or receiver cap conflict).

Q4. How can I verify fairness?
A4. Check history status, load numbers, and logs (rotation scope, skip reasons, threshold/cap checks).

## 中文

### 目的
- 當負載失衡時，建議把一筆案件從高負載工程師轉給較低負載工程師。

### 核心流程
1. 計算每位工程師即時負載。
2. 先套用 pending 歷史調整。
3. 找出超過門檻的工程師。
4. 先通知過載工程師：請在 IPS 將要轉出的 issue 指派給 Jonathan（Stage 1 trigger）。
5. 從 Jonathan queue 選一筆最新且符合條件的 IPS 案件。
6. 在同群組挑一位符合條件的接收者。
7. 發送提醒/建議信，並更新歷史紀錄。

### 主要規則
- 來源必須過載，且屬於 WiFi/BT 群組。
- 案件選擇：在過載工程師的案件中，先做歷史去重，再選「最新建立」且符合條件的 IPS。
- 接收者必須同群組、未超過接收上限，且不被 pending 條件阻擋。
- 接收上限規則：目前上限為 9，所以「10 件以上」不會被列入接收候選。
- `close-pending` 的 IPS 案件不再納入新建議。

### 公平性規則
- 近期接收者只看 pending/realized。
- 近期接收者以群組範圍計算。
- 近期視窗用「天數」計算（預設 10 天）。
- 超過無活動天數（預設 10 天）會重置輪轉。

### 為什麼團隊會覺得公平
- 同一套標準：所有人都用同一門檻、同一接收上限、同一群組規則。
- 沒有隱性偏好：候選排序以負載優先，平手時用固定規則。
- 避免重複承擔：近期接收者阻擋可避免同一人被連續指派。
- 舊紀錄不會永久影響：超過 10 天視窗後，舊紀錄自然失效。
- 避免不必要轉派：close-pending 與已不需要轉派的案件會被過濾。

### 透明化檢查點
- 每次建議都可用 3 個數字解釋：來源負載、接收負載、門檻/上限。
- 歷史完整保留：pending、realized、diverted、cancelled。
- 日誌會顯示輪轉範圍、近期接收者與跳過原因。

### Pending 提醒規則
- 只有在「現在仍需要轉派」才發提醒：
  - 來源仍高於門檻
  - 來源負載仍高於接收者
  - 接收者仍在上限內

### 歷史狀態
- pending：已建議，待執行
- realized：已轉到指定接收者
- diverted：已轉走，但不是指定接收者
- cancelled：手動或規則取消

### 郵件行為
- dry-run：只產生/記錄內容，不寄信
- send mode：依當前狀態寄出提醒/資訊/建議

### 常見問答（Q&A）
Q1. 為什麼這次是我被選為接收者？
A1. 因為你與來源同群組、未超過接收上限，且在符合條件者中屬於低負載。

Q2. 為什麼我負載低卻沒有被選？
A2. 常見原因：近期接收者視窗、pending 接收者限制、或群組不相符。

Q3. 為什麼某筆 pending 被取消？
A3. 可能是手動回滾，或依規則判定已不需要轉派（例如 close-pending 或接收上限衝突）。

Q4. 我要怎麼確認規則真的公平？
A4. 看歷史狀態、負載數字與日誌（輪轉範圍、跳過原因、門檻/上限判斷）。
