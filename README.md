# Huawei OLT Manager — EPON Auto-Configure Tool

## Install & Run

### Step 1: Python Install করুন (যদি না থাকে)
Python 3.8+ লাগবে। https://python.org থেকে download করুন।

### Step 2: Dependencies Install করুন
```
pip install -r requirements.txt
```

### Step 3: Run করুন
```
python app.py
```

### Step 4: Browser খুলুন
http://localhost:5000

---

## কীভাবে ব্যবহার করবেন

1. **OLT IP, Username, Password** দিয়ে Connect করুন
2. **Scan Unconfigured ONU** button চাপুন
3. List থেকে যে ONU configure করবেন সেটায় click করুন
4. ONT ID automatic সেট হবে (free ID দেখাবে)
5. **Configure This ONU** চাপুন — সব commands automatic run হবে!

---

## PON → Service VLAN Mapping
| PON | VLAN | PON | VLAN |
|-----|------|-----|------|
| 0   | 897  | 8   | 905  |
| 1   | 898  | 9   | 906  |
| 2   | 899  | 10  | 907  |
| 3   | 900  | 11  | 908  |
| 4   | 901  | 12  | 909  |
| 5   | 902  | 13  | 910  |
| 6   | 903  | 14  | 911  |
| 7   | 904  | 15  | 912  |

---

## System Requirements
- Windows 7/10/11 বা Linux
- Python 3.8+
- OLT-এর সাথে network connectivity
- SSH port 22 open থাকতে হবে
