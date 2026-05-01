#!/usr/bin/env python3
"""
Ishar Log Parser
================
Reads a MushClient log file and outputs the same CSVs as the
NPC Tracker and Item Examiner MushClient plugins:

  npcs_considered.csv   - NPCs conned
  npcs_descriptions.csv - NPC flavour text
  armor.csv             - Examined armor items
  weapons.csv           - Examined weapons

USAGE:
    python ishar_log_parser.py <logfile.txt>
    python ishar_log_parser.py          (scans all .txt files in same folder)

Merges with existing CSVs if present.
"""

import csv
import os
import re
import sys
from collections import defaultdict

# ===================== CONFIGURATION ========================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

ARMOR_FIELDS   = ['Name','Slot','AC','Stat1','Stat2','Special','Alignment','Disenchant','Loads','Location']
WEAPON_FIELDS  = ['Name','Type','Wield','Damage','Avg','Stat1','Stat2','Stat3','Special','Alignment','Disenchant','Loads','Location']
NPC_FIELDS     = ['Name','Level','Class','Race','Location','Loads']
DESC_FIELDS    = ['Name','Description']
CHEST_FIELDS     = ['Chest','Location','Contents']
# Container names containing any of these words are treated as chests
CHEST_KEYWORDS   = {'chest', 'coffer', 'crate', 'box', 'trunk', 'locker', 'cabinet', 'safe', 'vault'}
# ============================================================

SEP = '\xaf'  # MUD separator character

def is_estimate_level(lvl):
    """Return True if lvl is not a reliable solid number (estimate, unknown, or blank)."""
    s = str(lvl).strip()
    return not s or s == '?' or s.endswith('+') or s.endswith('-')

DIFFICULTY_MAP = {
    "Probably a waste of time":                      -4,
    "Easy":                                          -3,
    "Fairly easy":                                   -2,
    "A pretty worthy opponent":                      -1,
    "A worthy opponent":                              0,
    "You would need some luck":                       1,
    "You would need a lot of luck":                   2,
    "You'd need a lot of luck and great equipment!":  3,
    "Do you feel lucky, punk?":                       4,
    "Are you mad?":                                   5,
}

RACE_ALT_MAP = {
    "It looks odd, and unlike most other creatures of the lands": "Odd",
    "It has feathered wings":     "Bird",
    "It is a mammal":             "Mammal",
    "She is a mammal":            "Mammal",
    "It has both gills and feet": "Amphibian",
    "It is some sort of plant":   "Plant",
    "He pants and wags":          "Animal",
    "It bears a hard shell":      "Crustacean",
}

CLASS_KEYWORDS = ["magician","cleric","rogue","warrior","shaman","necromancer"]

HIDDEN_BONUSES = {
    "With it, the charms and wiles of others are laughable": "!Summon",
    "It removes you from sight, invisible to the unaided eye": "Invisibility",
    "It makes darkness seem but an illusion": "Infravision",
    "It hums in resonance with the life force of others nearby": "Detect life",
    "It makes you feel light as a feather": "Featherfall",
    "With this, the inner nature of man or beast is known": "Detect Alignment",
    "It sweeps a pleasant chill over your body": "Endure heat",
    "It renders the invisible visible to your eyes once more": "Detect invisible",
    "It fills you with a radiant warmth": "Endure cold",
    "It gives you insight into magical properties": "Detect magic",
}

ITEM_SKIP = [
    r"^It is", r"^It glows", r"^It requires", r"^It can ",
    r"^It will eventually", r"^You are", r"^It is worth",
    r"^It is shrouded", r"^Your class is", r"^It has been coated",
    r"^Rank:", r"^Within the",
    r"presents .+ to",   # NPC gift lines anywhere in the string
    r"^It gives you",    # bonus lines from item examiner (not the item itself)
    r"^You don't have",  # ability/cooldown messages
    r"^You try to",      # inventory peek messages
    r"^You follow",      # movement messages
    r" goes ",           # NPC movement during examine
]

ITEM_SKIP_BONUS = [
    "bound when returning from rent",
    "remaining",
    "It keeps you firmly",
    "It fills your lungs",
    "It seems empty",
    "It will quench",
    "Self only",
    "You could probably sell",
    "thrumming with power",
    "is anathema to you",
    "Moderate Defense",
    "promises to withstand",
    "greater mystic essence",
    "Within the Recipe",
    "Too high",
    "is no longer on cooldown",   # system/ability notifications
    "goes into effect",
    "wears off",
]

VALID_SLOTS = [
    "Head","Face","Neck","About","Body","Back","Wrist","Hands",
    "Finger","Wielding","Holding","Waist","Feet","Legs","Chest",
    "Arms","Shoulders","(w)Holding","Both hands","Mouth","Upper Body",
]

# ===================== HELPERS ==============================

def trim(s):
    return (s or "").strip()

def strip_article(s):
    s = trim(s)
    s = re.sub(r'^[Aa]n? ', '', s)
    s = re.sub(r'^[Tt]he ', '', s)
    return s[0].upper() + s[1:] if s else s

def clean_name(name):
    name = trim(name)
    stripped = re.sub(r'^[Aa]n? ', '', name)
    stripped = re.sub(r'^[Tt]he ', '', stripped)
    caps = sum(1 for w in stripped.split() if w and w[0].isupper())
    if caps >= 2:
        return stripped
    return stripped.capitalize() if stripped else stripped

def clean_class(cls):
    cls = trim(cls).lower()
    if 'classless' in cls: return 'Classless'
    for kw in CLASS_KEYWORDS:
        if kw in cls: return kw.capitalize()
    return cls.capitalize()

# Known false-positive location strings (skill names, system strings, etc.)
_LOCATION_NOISE = {
    "Nothing.", "You couldn't see anything.",
    "Calculated Assault", "Disengage", "Distract", "Evade",
    "Exploit Weakness", "Fatal Instinct", "Hide", "Kick",
    "Neural Spike", "Trip", "Effects:",
}

def is_valid_location(loc):
    if not loc or loc.strip() == "": return False
    loc = loc.strip()
    if loc in _LOCATION_NOISE: return False
    if re.search(r"couldn't see anything", loc): return False
    # Reject shop price lines: "A potion of X for 36000 obsidian coins."
    if re.search(r'\bfor\b.*\d+.*\bobsidian coins\b', loc, re.I): return False
    # Reject event/timer strings (digits + time words, e.g. "Challenges Cycled! - 3 days")
    if re.search(r'\d', loc) and re.search(r'\b(day|hour|min)\b', loc): return False
    # Reject lines with exclamation marks or parentheses (event announcements/bonuses)
    if '!' in loc: return False
    if '(' in loc: return False
    # Hard length cap — real zone strings are short; room description sentences are long
    if len(loc) > 60: return False
    # Reject lines that read like sentences (contain common verbs/conjunctions mid-line)
    if re.search(r'\b(had|has|have|is|was|were|which|that|and the|of the|with a|with the)\b', loc): return False
    if " - " in loc: return True
    return len(loc.split()) <= 4

def merge_locations(existing, new_loc):
    if not new_loc: return existing
    if not existing: return new_loc
    seen = set()
    parts = []
    for loc in existing.split(","):
        l = loc.strip()
        if l: seen.add(l); parts.append(l)
    for loc in new_loc.split(","):
        l = loc.strip()
        if l and l not in seen:
            seen.add(l); parts.append(l)
    return ", ".join(parts)

def make_npc_key(name, level):
    lvl_m = re.match(r'^(\d+)', str(level))
    lvl_num = int(lvl_m.group(1)) if lvl_m else 0
    if lvl_num >= 20:
        return f"{name} ({level})"
    return name

def parse_ac(ac_str):
    m = re.search(r'\((\d+)\*(\d+)\+(\d+)', ac_str)
    if m: return str(int(m.group(1)) * int(m.group(2)) + int(m.group(3)))
    m = re.search(r'\((\d+)\*(\d+)-(\d+)', ac_str)
    if m: return str(int(m.group(1)) * int(m.group(2)) - int(m.group(3)))
    m = re.search(r'\((\d+)\*(\d+)', ac_str)
    if m: return str(int(m.group(1)) * int(m.group(2)))
    return ac_str

def parse_one_dice(s):
    m = re.search(r'(\d+)d(\d+)\+(\d+)', s) or re.search(r'(\d+)d(\d+)', s)
    if m:
        d, si = int(m.group(1)), int(m.group(2))
        b = int(m.group(3)) if len(m.groups()) > 2 else 0
        notation = f"{d}d{si}+{b}" if b > 0 else f"{d}d{si}"
        return d + b, d * si + b, notation
    return 0, 0, None

def parse_damage(dmg_str):
    parts, total_min, total_max = [], 0, 0
    for expr in re.findall(r'\d+d\d+[+\d]*', dmg_str):
        mn, mx, notation = parse_one_dice(expr)
        total_min += mn; total_max += mx
        if notation: parts.append(notation)
    if total_max > 0:
        return '+'.join(parts), f"{(total_min + total_max) / 2:.1f}"
    return None, None

def translate_bonus(line, is_weapon=False):
    t = trim(line).rstrip('.')
    for sb in ITEM_SKIP_BONUS:
        if sb in t: return None, None
    for k, v in HIDDEN_BONUSES.items():
        if t[:len(k)] == k: return "hidden", v
    m = re.search(r'\(([^)]+)\)', line)
    if m:
        val = m.group(1).strip()
        if is_weapon and not re.match(r'^[+\-]', val): return None, None
        return "stat", val
    if is_weapon: return None, None
    # For armor: only accept lines that look like genuine stat bonuses
    # (contain a +/- sign, a % symbol, or a recognisable stat keyword)
    if re.search(r'[+\-]|%|\b(hit|melee|spell|heal|attack|damage|endurance|strength|dexterity|intelligence|wisdom|constitution|charisma|speed|armor|defense)\b', t, re.I):
        return "stat", t
    return None, None

def infer_slot(name):
    n = name.lower()
    if re.search(r'\bring\b', n): return 'Finger'
    if any(x in n for x in ['necklace','pendant','choker','amulet','collar','scarf']): return 'Neck'
    if any(x in n for x in ['earring','stud','earrings']): return 'Face'
    if any(x in n for x in ['bracelet','bangle','bracer','wristguard','wristband','armband']): return 'Wrist'
    if any(x in n for x in ['pack','backpack','satchel','knapsack','rucksack']): return 'Back'
    if any(x in n for x in ['belt','swordbelt','girdle','sash','cincture']): return 'Waist'
    return None

def csv_escape(s):
    s = str(s or "")
    if not s: return "-"
    if any(c in s for c in [',', '"', '\n', '\r']):
        s = '"' + s.replace('"', '""') + '"'
    return s

def item_skip_match(t):
    """Check ITEM_SKIP patterns; use re.search for non-anchored patterns."""
    for p in ITEM_SKIP:
        fn = re.match if p.startswith('^') else re.search
        if fn(p, t):
            return True
    return False

# ===================== CSV I/O ==============================

def load_csv_dict(path, key_fields):
    """Load a CSV into a dict keyed by tuple of key_fields."""
    result = {}
    if not os.path.exists(path): return result
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            key = tuple(row.get(k, '') for k in key_fields)
            result[key] = row
    return result

def write_csv(path, rows, fieldnames):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            for field in fieldnames:
                row.setdefault(field, '-')
            writer.writerow(row)

# ===================== LOG PARSER ===========================

class LogParser:
    def __init__(self):
        self.player_level = 0          # 0 = not yet known; triggers warning on first con
        self.char_levels = {}
        self.in_char_table = False
        self.known_players = set()

        # NPC state
        self.npcs = {}   # key -> record
        self.descriptions = {}  # name -> description

        # Item state
        self.armor = {}    # name -> record
        self.weapons = {}  # name -> record
        self.chests = []      # list of {Chest, Location, Item} rows
        self._seen_chest_triples = set()

        # Runtime state
        self.current_area = ""
        self.last_known_area = ""
        self.prev_line = ""

        self._reset_con()
        self._reset_item()
        self._reset_look()

    # ---- Con state ----
    def _reset_con(self):
        self.con_pending = False
        self.con_difficulty = None
        self.con_name = None
        self.con_key = None
        self.con_class = None
        self.con_race = None
        self.con_location = ""

    # ---- Look state ----
    def _reset_look(self):
        self.look_name = None
        self.look_key = None
        self.wearing_active = False
        self.container_active = False
        self.container_name = None
        self.chest_active = False
        self.look_items = []
        self.desc_lines = []

    # ---- Item state ----
    def _reset_item(self):
        self.item_active = False
        self.item_name = None
        self.item_slot = None
        self.item_ac = None
        self.item_damage = None
        self.item_wield = None
        self.item_stat_bonuses = []
        self.item_stat3 = None
        self.item_hidden = []
        self.item_alignment = None
        self.item_disenchant = None
        self.item_desc = []
        self.item_is_weapon = False

    # ---- Helpers ----
    def _make_npc_key(self, name, level):
        return make_npc_key(name, level)

    def _commit_con(self):
        if not self.con_pending or not self.con_name: return
        name = self.con_name
        level = self.con_difficulty or ""
        is_estimate = is_estimate_level(level)

        loc = self.con_location or ""
        if not is_valid_location(loc): loc = ""

        # NPC key: (name, location) -- each unique appearance is its own record
        key = (name, loc)

        # If this is an estimate and a solid record already exists for this
        # name+location pair, use that existing record instead
        if is_estimate and key not in self.npcs:
            for k, v in self.npcs.items():
                if k[0] == name and k[1] == loc and \
                   not is_estimate_level(v.get('Level','')):
                    self.con_key = k
                    self._reset_con()
                    return

        if key in self.npcs:
            # Update level if existing is an estimate and new is solid
            existing_lvl = str(self.npcs[key].get('Level', ''))
            if is_estimate_level(existing_lvl) and not is_estimate:
                self.npcs[key]['Level'] = level
        else:
            self.npcs[key] = {
                'Name': name, 'Level': level,
                'Class': self.con_class or '',
                'Race': self.con_race or '',
                'Location': loc, 'Loads': '',
            }

        self.con_key = key
        self._reset_con()

    def _commit_look(self):
        key = self.look_key or self.con_key
        if not key and self.look_name:
            # Fallback: find any record whose Name matches look_name
            for k, v in self.npcs.items():
                if v.get('Name') == self.look_name:
                    key = k; break
        name = self.look_name

        if key and key in self.npcs and self.look_items:
            existing = self.npcs[key].get('Loads', '')
            # Rebuild seen set honouring comma-in-name items (e.g. "Robust, linen bandana")
            seen = set()
            parts = []
            if existing and existing != '-':
                tokens = [t.strip() for t in existing.split(',')]
                current = ''
                for tok in tokens:
                    if not tok:
                        continue
                    elif not current:
                        current = tok
                    elif tok and tok[0].islower():
                        current = current + ', ' + tok
                    else:
                        if current:
                            seen.add(current.lower())
                            parts.append(current)
                        current = tok
                if current:
                    seen.add(current.lower())
                    parts.append(current)
            added = False
            for item in self.look_items:
                item = trim(item)
                if item and item.lower() not in seen:
                    parts.append(item)
                    seen.add(item.lower())
                    added = True
            if added:
                self.npcs[key]['Loads'] = ', '.join(parts)

        # Only update location if look_name matches the NPC in key
        # Key is now a (name, location) tuple
        if key and key in self.npcs and name:
            keyed_name = (self.npcs[key].get('Name') or (key[0] if isinstance(key, tuple) else key)).lower()
            if keyed_name == name.lower():
                existing_loc = self.npcs[key].get('Location', '')
                room_loc = self.last_known_area or ''
                # Only set location if the record has none yet
                if is_valid_location(room_loc) and not existing_loc:
                    self.npcs[key]['Location'] = room_loc

        self.wearing_active = False
        self.container_active = False
        self.chest_active = False
        self.container_name = None
        self.look_items = []
        self.desc_lines = []
        self.look_name = None
        self.look_key = None

    def _commit_item(self):
        if not self.item_active or not self.item_name: return
        name = self.item_name

        # Infer slot from name if not found
        if not self.item_slot:
            self.item_slot = infer_slot(name)

        if self.item_is_weapon:
            if name in self.weapons: self._reset_item(); return
            dmg, avg = parse_damage(self.item_damage) if self.item_damage else (None, None)
            wt = re.match(r'^(\w+) weapon', self.item_wield or '')
            wtype = wt.group(1).capitalize() if wt else ''
            hands = "Two hand" if re.search(r'two hand|both hands', self.item_wield or '', re.I) else "One hand"
            self.weapons[name] = {
                'Name': name, 'Type': wtype, 'Wield': hands,
                'Damage': dmg or '-', 'Avg': avg or '-',
                'Stat1': self.item_stat_bonuses[0] if len(self.item_stat_bonuses) > 0 else '-',
                'Stat2': self.item_stat_bonuses[1] if len(self.item_stat_bonuses) > 1 else '-',
                'Stat3': self.item_stat3 or '-',
                'Special': ', '.join(self.item_hidden) or '-',
                'Alignment': self.item_alignment or '-',
                'Disenchant': self.item_disenchant or '-',
                'Loads': '-', 'Location': '-',
            }
        else:
            if name in self.armor: self._reset_item(); return
            self.armor[name] = {
                'Name': name,
                'Slot': (self.item_slot or '').capitalize(),
                'AC': parse_ac(self.item_ac) if self.item_ac else '-',
                'Stat1': self.item_stat_bonuses[0] if len(self.item_stat_bonuses) > 0 else '-',
                'Stat2': self.item_stat_bonuses[1] if len(self.item_stat_bonuses) > 1 else '-',
                'Special': ', '.join(self.item_hidden) or '-',
                'Alignment': self.item_alignment or '-',
                'Disenchant': self.item_disenchant or '-',
                'Loads': '-', 'Location': '-',
            }
        self._reset_item()

    def process_line(self, line):
        line = line.replace('\r', '').replace('\x0d', '')
        t = trim(line)

        # ---- Who list: capture player names ----
        m = re.search(chr(0xb0) + r'\s+(\w+)\s*', line)
        if m:
            self.known_players.add(m.group(1).lower())
            return

        # ---- Login: character table ----
        if re.search(r'Name.*Class.*Level.*Remorts', line):
            self.in_char_table = True; return
        if self.in_char_table:
            m = re.match(r'^\s+(\w+)\s+\S+\s+(\d+)\s+\d+', line)
            if m: self.char_levels[m.group(1).lower()] = int(m.group(2))
        m = re.match(r'^Loading (\w+) into the world', line)
        if m:
            self.in_char_table = False
            lvl = self.char_levels.get(m.group(1).lower())
            if lvl:
                self.player_level = lvl
            else:
                print(f"  \033[91mWARNING: No level found for {m.group(1)} — type `sc` in-game before conning.\033[0m")
            return

        # ---- Level detection via sc ----
        m = re.search(r'Level\s*:\s*(\d+)', line)
        if m and 'Aligned' in line:
            self.player_level = int(m.group(1)); return

        # ---- Level up ----
        if 'advance your status' in line:
            self.player_level += 1; return

        # ---- Prompt ----
        if re.match(r'^\d+H \d+M', line):
            if self.last_known_area == "" or self.current_area:
                self.last_known_area = self.current_area
            if self.wearing_active or self.chest_active:
                self._commit_look()
            if self.con_pending and self.con_name:
                self._commit_con()
            elif self.con_pending and not self.con_name:
                self._reset_con()
            if self.item_active:
                self._commit_item()
            self.desc_lines = []
            self.prev_line = line
            return

        # ---- Location tracking ----
        if re.match(r'^   [A-Z]', line):
            candidate = t
            if is_valid_location(candidate):
                prev = trim(self.prev_line)
                # Valid room-name prev: starts with capital, short, no prompt chars
                prev_ok = (
                    re.match(r'^[A-Z]', prev)
                    and len(prev) < 60
                    and not re.match(r'^\d+H', prev)
                    and '>' not in prev
                    # Reject command-output headers (end with colon)
                    and not prev.endswith(':')
                    # Reject "You ..." lines (movement messages, inventory peeks, etc.)
                    and not prev.startswith('You ')
                    # Reject "Global ..." lines (event headers)
                    and not prev.startswith('Global ')
                    # Reject sc armor condition lines (contain ' - ' with condition words)
                    and not re.search(r' - (Perfect|Worthless|Damaged|Worn|Cracked|Good|Bad|Excellent|Fair|Poor)', prev, re.I)
                )
                if prev_ok:
                    self.current_area = candidate

        # ---- Wearing: header ----
        if t == "Wearing:":
            self.wearing_active = True
            self.look_items = []
            self.prev_line = line; return

        # ---- Wearing: Nothing ----
        if self.wearing_active and t == "Nothing.":
            self.wearing_active = False
            self.look_items = []
            self.prev_line = line; return

        # ---- Chest/Container: "Inside the X you see:" ----
        m = re.match(r'^Inside (.+) you see:', t)
        if m:
            if self.wearing_active:
                self._commit_look()
            self.look_name = None
            self.con_key = None
            raw_name = strip_article(m.group(1).strip())
            is_chest = any(kw in raw_name.lower() for kw in CHEST_KEYWORDS)
            self.chest_active = is_chest
            self.container_active = False
            self.container_name = raw_name if is_chest else None
            self.look_items = []
            self.prev_line = line; return

        # ---- Chest: item lines ----
        if self.chest_active:
            if t == "":
                self.chest_active = False
                self.container_name = None
                self.look_items = []
            else:
                item = re.sub(r'^-?\d+-?\s*', '', t)
                item = item.replace('~', '').split('...')[0]
                item = trim(item)
                # Skip coin lines and Nothing.
                if item and item != "Nothing." and 'coins' not in item.lower():
                    row = {
                        'Chest':    self.container_name or '',
                        'Location': self.last_known_area or '',
                        'Item':     strip_article(item),
                    }
                    triple = (row['Chest'], row['Location'], row['Item'])
                    if not hasattr(self, '_seen_chest_triples'):
                        self._seen_chest_triples = set()
                    if triple not in self._seen_chest_triples:
                        self._seen_chest_triples.add(triple)
                        self.chests.append(row)
            self.prev_line = line; return

        # ---- Wearing: item line ----
        if self.wearing_active:
            for slot in VALID_SLOTS:
                if slot + " " + SEP in line or slot + SEP in line:
                    item = re.search(SEP + r'\s*(.+)$', line)
                    if item:
                        item_text = trim(item.group(1)).replace('~', '').replace('...it\'s unclaimed!', '')
                        item_text = trim(item_text)
                        if item_text:
                            self.look_items.append(strip_article(item_text))
                    break
            self.prev_line = line; return

        # ---- NPC standing/sitting/etc ----
        POSE_PATS = [
            r'^\s*(.*?)\s+is [Aa]n? .+ standing here',
            r'^\s*(.*?)\s+is [Aa]n? .+ sitting here',
            r'^\s*(.*?)\s+is [Aa]n? .+ resting here',
            r'^\s*(.*?)\s+is [Aa]n? .+ sleeping here',
            r'^\s*(.*?)\s+is [Aa]n? .+ kneeling here',
        ]
        npc_name = None
        for pat in POSE_PATS:
            pm = re.match(pat, line)
            if pm:
                npc_name = pm.group(1).strip(); break

        if npc_name:
            cname = clean_name(npc_name)
            if cname.lower() not in self.known_players:
                self.look_name = cname
                # Clear con_key if it's for a different NPC
                # con_key is now a (name, location) tuple
                if self.con_key:
                    keyed_name = self.con_key[0] if isinstance(self.con_key, tuple) else self.con_key
                    if keyed_name != cname:
                        self.look_key = None
                    else:
                        self.look_key = self.con_key
                # Save description
                if self.desc_lines:
                    desc = ' '.join(self.desc_lines)
                    if cname not in self.descriptions:
                        self.descriptions[cname] = desc
                    self.desc_lines = []
            else:
                # It's a known player — clear all look state so their
                # wearing block cannot be credited to the last-conned NPC
                self.look_name = None
                self.look_key = None
                self.con_key = None
                self.desc_lines = []
            self.prev_line = line; return

        # ---- Description lines (before standing line) ----
        if not self.wearing_active and not self.con_pending and not self.item_active:
            if t and not re.match(r'^\d+H', line) and not re.match(r'^\*>', t) \
               and not re.match(r'^\.\.\.', t) and not t.startswith('~') \
               and 'You have gained' not in t and 'You are' not in t \
               and '  Wearing:' not in t and not is_valid_location(t):
                self.desc_lines.append(t)

        # ---- Con: difficulty ----
        if not self.con_pending:
            if 'You ARE mad' in line:
                self.con_pending = True
                if self.player_level == 0:
                    print("  \033[91mWARNING: player_level unknown (type `sc` first). Con level will be inaccurate.\033[0m")
                    self.con_difficulty = "?"
                else:
                    self.con_difficulty = str(self.player_level + 6) + "+"
                self.con_location = self.last_known_area
                self.desc_lines = []
                self.prev_line = line; return
            # "A waste of time" (but NOT "Probably a waste of time") = level- estimate
            t_line = trim(line)
            if t_line.startswith("A waste of time") and not t_line.startswith("Probably a waste of time"):
                self.con_pending = True
                if self.player_level == 0:
                    print("  \033[91mWARNING: player_level unknown (type `sc` first). Con level will be inaccurate.\033[0m")
                    self.con_difficulty = "?"
                else:
                    self.con_difficulty = str(self.player_level - 5) + "-"
                self.con_location = self.last_known_area
                self.desc_lines = []
                self.prev_line = line; return
            for phrase, offset in DIFFICULTY_MAP.items():
                if t.startswith(phrase):
                    self.con_pending = True
                    if self.player_level == 0:
                        print("  \033[91mWARNING: player_level unknown (type `sc` first). Con level will be inaccurate.\033[0m")
                        self.con_difficulty = "?"
                    else:
                        self.con_difficulty = str(self.player_level + offset)
                    self.con_location = self.last_known_area
                    self.desc_lines = []
                    self.prev_line = line; return

        # ---- Con: name + class ----
        if self.con_pending and not self.con_name:
            m = re.match(r'^(.+) is [Aa]n? (.+)\.$', t)
            if m:
                self.con_name = clean_name(m.group(1))
                self.con_class = clean_class(m.group(2))
                self.prev_line = line; return

        # ---- Con: race ----
        if self.con_pending and not self.con_race:
            m = re.search(r'appears to be of (.+) origin', t)
            if m:
                self.con_race = m.group(1).strip().capitalize()
                self.prev_line = line; return
            for pattern, label in RACE_ALT_MAP.items():
                if t.startswith(pattern):
                    self.con_race = label
                    self.prev_line = line; return

        # ---- Con: XP line (end of con) ----
        if self.con_pending and t.startswith("You will receive"):
            self._commit_con()
            self.prev_line = line; return

        # ===================== ITEM EXAMINER ====================

        ITEM_LINE = r'^(.+) is an? .+item,? ?(.*)'
        WEAP_LINE = r'^([^I].+) is an? .+weapon,? ?(.*)'
        AC_PATS = [
            (r'^It is an? (.+) piece of .+ worn on (?:the |a |an |your )?(.+)\.', None),
            (r'^It is an? (.+) piece of .+ worn around (?:the |a |an |your )?(.+)\.', None),
            (r'^It is an? (.+) piece of .+ worn about (?:the |a |an |your )?(.+)\.', 'About'),
            (r'^It is an? (.+) piece of .+ worn over (?:the |a |an |your )?(.+)\.', None),
            (r'^It is an? (.+) piece of .+ held in the (.+)\.', 'held'),
        ]
        DMG_LINE = r'^It is an? (.+\d+d\d+.+damage.+), (.+weapon.+)\.'

        # Description capture before item name line
        if not self.item_active:
            is_item = re.match(ITEM_LINE, t) or re.match(WEAP_LINE, t)
            if is_item: pass
            elif t == "": self.item_desc = []
            elif not item_skip_match(t):
                self.item_desc.append(t)

        # Item name line — but never fire on "X presents Y to you" or "It gives you..."
        m = re.match(ITEM_LINE, t)
        iw = False
        if not m:
            m = re.match(WEAP_LINE, t)
            if m: iw = True
        if m:
            # Reject NPC gift lines: "Waysl presents a mace to you" matches ITEM_LINE
            if re.search(r'\bpresents\b.+\bto\b|\bgives\b.+\bto\b', t, re.I):
                self.prev_line = line; return
            self._reset_item()
            self.item_active = True
            self.item_name = clean_name(m.group(1))
            self.item_is_weapon = iw
            self.prev_line = line; return

        if not self.item_active:
            self.prev_line = line; return

        # Disenchant
        m = re.match(r'^Disenchant:\s*(\d+)', t)
        if m:
            self.item_disenchant = m.group(1)
            self.prev_line = line; return

        # Weapon damage (before AC to avoid false match)
        m = re.match(DMG_LINE, t)
        if m:
            self.item_damage = m.group(1).strip()
            self.item_wield = m.group(2).strip()
            self.prev_line = line; return

        # AC / slot
        for pat, slot_override in AC_PATS:
            m = re.match(pat, t)
            if m:
                self.item_ac = m.group(1).strip()
                raw_slot = slot_override or m.group(2).strip()
                # "held in the hand, worn on the back, or worn as a shield"
                if re.search(r'worn as a shield', t, re.I):
                    self.item_slot = 'Shield'
                elif 'hand or worn' in raw_slot or 'hand, worn' in t:
                    self.item_slot = 'held'
                elif ' or worn ' in t:
                    s1 = re.match(r'^(\w+)', raw_slot)
                    s2 = re.search(r'.+or worn \w+ \w+ (\w+)', t)
                    if s1 and s2:
                        self.item_slot = s1.group(1) + "/" + s2.group(1)
                    else:
                        self.item_slot = raw_slot
                elif slot_override:
                    self.item_slot = slot_override
                else:
                    # Normalise raw slot word: "head" → "Head", "body" → "Body" etc.
                    self.item_slot = raw_slot.split()[0].capitalize() if raw_slot else raw_slot
                self.prev_line = line; return

        # Held slot from "You can hold it"
        if t.startswith("You can hold it in your hand") and not self.item_slot:
            self.item_slot = "held"
            self.prev_line = line; return

        # Backstab
        if t.startswith("It can also be used to backstab"):
            self.item_wield = (self.item_wield or "") + " (can backstab)"
            self.prev_line = line; return

        # Two-handed critical
        if re.match(r'^A two-handed stance', t):
            crit = re.search(r'\(([^)]+)\)', t)
            if crit: self.item_stat3 = crit.group(1)
            self.prev_line = line; return

        # Light source
        if 'light left in it' in t:
            self.item_hidden.append("Light")
            self.prev_line = line; return

        # Alignment
        if 'holy aura' in t or 'blessed by' in t:
            self.item_alignment = "!E"; self.prev_line = line; return
        if 'dark aura' in t or 'evil aura' in t or 'unholy aura' in t:
            self.item_alignment = "!G"; self.prev_line = line; return

        # Skip metadata
        if item_skip_match(t) or not t:
            self.prev_line = line; return

        # Bonuses
        btype, bval = translate_bonus(t, self.item_is_weapon)
        if btype == "hidden":
            self.item_hidden.append(bval)
        elif btype == "stat" and bval and len(bval) <= 60:
            if len(self.item_stat_bonuses) < 2:
                self.item_stat_bonuses.append(bval)

        self.prev_line = line

    def parse_file(self, filepath):
        print(f"  Parsing: {filepath}")
        with open(filepath, encoding='latin-1') as f:
            for line in f:
                self.process_line(line)
        # Commit any open states at EOF
        if self.wearing_active: self._commit_look()
        if self.con_pending and self.con_name: self._commit_con()
        if self.item_active: self._commit_item()


# ===================== MAIN =================================

def load_existing_csv(path, fieldnames):
    rows = {}
    if not os.path.exists(path): return rows
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rows[row.get('Name', '')] = row
    return rows

def merge_and_write(path, new_data, fieldnames):
    existing = load_existing_csv(path, fieldnames)
    merged = dict(existing)
    added = updated = 0
    for name, record in new_data.items():
        display_name = record.get('Name', name)
        if display_name not in merged:
            merged[display_name] = record
            added += 1
        else:
            # Merge location
            ex_loc = merged[display_name].get('Location', '')
            new_loc = record.get('Location', '')
            if is_valid_location(new_loc):
                ml = merge_locations(ex_loc, new_loc)
                if ml != ex_loc:
                    merged[display_name]['Location'] = ml
                    updated += 1
            # Merge loads
            ex_loads = merged[display_name].get('Loads', '')
            new_loads = record.get('Loads', '')
            if new_loads and new_loads != '-':
                ml = merge_locations(ex_loads if ex_loads != '-' else '', new_loads if new_loads != '-' else '')
                if ml != ex_loads:
                    merged[display_name]['Loads'] = ml
                    updated += 1
    write_csv(path, list(merged.values()), fieldnames)
    return added, updated


# =====================================================================
# PHASE 2: LOAD LINKER
# Cross-references NPC Loads against armor/weapons CSVs
# =====================================================================

# Column order for armor/weapons after load linking (Loads + Location prepended after Name)
ARMOR_LINKED_FIELDS   = ['Name','Loads','Slot','AC','Stat1','Stat2','Special','Alignment','Disenchant','Location']
WEAPONS_LINKED_FIELDS = ['Name','Loads','Type','Wield','Damage','Avg','Stat1','Stat2','Stat3','Special','Alignment','Disenchant','Location']


def normalise(s):
    """Lowercase, strip leading articles and punctuation for fuzzy matching."""
    s = s.strip().lower()
    s = re.sub(r'^(a|an|the)\s+', '', s)
    return re.sub(r'[^a-z0-9 ]', '', s)


def build_npc_load_map(npc_rows):
    """
    Returns dict: normalised item name -> list of (npc_name, location).
    Handles multiple NPCs loading the same item.
    """
    load_map = {}
    for row in npc_rows:
        npc_name = row.get('Name', '').strip()
        location = row.get('Location', '').strip()
        loads    = row.get('Loads', '').strip()
        if not loads or loads == '-':
            continue
        for item in loads.split(','):
            item = item.strip()
            if not item or item == '-':
                continue
            key = normalise(item)
            load_map.setdefault(key, []).append((npc_name, location))
    return load_map


def link_items(item_rows, load_map, label):
    """Fill Loads and Location columns in item rows from NPC load map."""
    updated = 0
    for row in item_rows:
        row.setdefault('Loads', '-')
        row.setdefault('Location', '-')
        name = row.get('Name', '').strip()
        if not name:
            continue
        key = normalise(name)
        if key not in load_map:
            continue
        matches = load_map[key]

        npc_names, locations = [], []
        seen_npcs, seen_locs = set(), set()
        for npc_name, location in matches:
            if npc_name and npc_name not in seen_npcs:
                npc_names.append(npc_name)
                seen_npcs.add(npc_name)
            if location and location not in seen_locs \
               and location not in ('-', '', 'Nothing.') \
               and "couldn't see anything" not in location:
                locations.append(location)
                seen_locs.add(location)

        new_loads = ', '.join(npc_names) if npc_names else '-'
        new_loc   = ', '.join(locations) if locations else '-'

        changed = False
        # Merge Loads
        if row['Loads'] in ('-', '', None) and new_loads != '-':
            row['Loads'] = new_loads
            changed = True
        elif new_loads != '-' and new_loads != row['Loads']:
            existing = {n.strip() for n in row['Loads'].split(',') if n.strip() != '-'}
            merged   = existing | {n.strip() for n in new_loads.split(',')}
            row['Loads'] = ', '.join(sorted(merged))
            changed = True
        # Merge Location
        if row['Location'] in ('-', '', None) and new_loc != '-':
            row['Location'] = new_loc
            changed = True
        elif new_loc != '-' and new_loc != row['Location']:
            existing_locs = {l.strip() for l in row['Location'].split(',') if l.strip() != '-'}
            merged_locs   = existing_locs | {l.strip() for l in new_loc.split(',')}
            row['Location'] = ', '.join(sorted(merged_locs))
            changed = True
        if changed:
            updated += 1
    return updated


def run_link_loads():
    print("\nLoad Linker")
    print("=" * 40)

    npc_path    = os.path.join(SCRIPT_DIR, 'npcs_considered.csv')
    armor_path  = os.path.join(SCRIPT_DIR, 'armor.csv')
    weapons_path = os.path.join(SCRIPT_DIR, 'weapons.csv')

    if not os.path.exists(npc_path):
        print("  npcs_considered.csv not found — skipping load linking.")
        return

    with open(npc_path, newline='', encoding='utf-8-sig') as f:
        npc_rows = list(csv.DictReader(f))
    load_map = build_npc_load_map(npc_rows)
    print(f"  {len(npc_rows)} NPCs, {len(load_map)} unique loadable items")

    for path, fields, label in [
        (armor_path,   ARMOR_LINKED_FIELDS,   'armor'),
        (weapons_path, WEAPONS_LINKED_FIELDS, 'weapons'),
    ]:
        if not os.path.exists(path):
            print(f"  {label}.csv not found — skipping.")
            continue
        with open(path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        n = link_items(rows, load_map, label)
        write_csv(path, rows, fields)
        print(f"  {label}: {n} items updated -> {path}")


# =====================================================================
# PHASE 3: WIKI GENERATOR — one file per location
# =====================================================================

WIKI_OUT_DIR = os.path.join(SCRIPT_DIR, 'location_pages')


def safe_filename(loc):
    return re.sub(r'[^\w\s-]', '', loc).strip().replace(' ', '_') + '.txt'


def wdash(val):
    v = str(val).strip()
    return '' if v in ('-', '') else v


def wikilink(val):
    val = str(val).strip()
    return '' if not val or val == '-' else f'[[{val}]]'


def _strip_article(s):
    s = s.strip()
    s = re.sub(r'^[Aa]n? ', '', s)
    s = re.sub(r'^[Tt]he ', '', s)
    return s[0].upper() + s[1:] if s else s


def wikilink_list(val):
    val = str(val).strip()
    if not val or val == '-':
        return ''
    items = [_strip_article(i) for i in val.split(',') if i.strip()]
    return ', '.join(f'[[{i}]]' for i in items if i)


def get_locations(val):
    if not val or val.strip() in ('-', ''):
        return []
    return [l.strip() for l in val.split(',')
            if l.strip() and l.strip() not in ('-', 'Nothing.', "You couldn't see anything.")]


def build_location_data(armor_rows, weapon_rows, npc_rows, chest_rows=None):
    from collections import defaultdict
    data = defaultdict(lambda: {'armor': [], 'weapons': [], 'npcs': [], 'chests': {}})
    for row in armor_rows:
        for loc in get_locations(row.get('Location', '')):
            data[loc]['armor'].append(row)
    for row in weapon_rows:
        for loc in get_locations(row.get('Location', '')):
            data[loc]['weapons'].append(row)
    for row in npc_rows:
        loc = row.get('Location', '').strip()
        if loc and loc not in ('-', '', 'Nothing.', "You couldn't see anything."):
            data[loc]['npcs'].append(row)
    for row in (chest_rows or []):
        loc = row.get('Location', '').strip()
        if loc and loc not in ('-', '', 'Nothing.', "You couldn't see anything."):
            chest_name = row.get('Chest', '')
            contents = row.get('Contents', '')
            if chest_name:
                items = {i.strip() for i in contents.split(',') if i.strip()}
                data[loc]['chests'].setdefault(chest_name, set()).update(items)
    return data


def generate_location_page(loc, items):
    wiki = f"= {loc} =\n\n"

    if items['npcs']:
        wiki += "== NPCs ==\n"
        wiki += (
            '{| class="wikitable sortable" style="width:100%"\n'
            '! Name !! Level !! Class !! Race !! Loads\n'
        )
        for r in sorted(items['npcs'], key=lambda x: x.get('Name', '')):
            wiki += (
                f"|-\n"
                f"| {wikilink(r.get('Name',''))} "
                f"|| {wdash(r.get('Level',''))} "
                f"|| {wikilink(r.get('Class',''))} "
                f"|| {wikilink(r.get('Race',''))} "
                f"|| {wikilink_list(r.get('Loads',''))}\n"
            )
        wiki += "|}\n\n"

    if items['armor']:
        wiki += "== Armor ==\n"
        wiki += (
            '{| class="wikitable sortable" style="width:100%"\n'
            '! Name !! Slot !! AC !! Stat 1 !! Stat 2 !! Special !! Align !! Disenchant !! Loaded by\n'
        )
        for r in sorted(items['armor'], key=lambda x: x.get('Name', '')):
            wiki += (
                f"|-\n"
                f"| {wikilink(r.get('Name',''))} "
                f"|| {wdash(r.get('Slot',''))} "
                f"|| {wdash(r.get('AC',''))} "
                f"|| {wdash(r.get('Stat1',''))} "
                f"|| {wdash(r.get('Stat2',''))} "
                f"|| {wdash(r.get('Special',''))} "
                f"|| {wdash(r.get('Alignment',''))} "
                f"|| {wdash(r.get('Disenchant',''))} "
                f"|| {wikilink_list(r.get('Loads',''))}\n"
            )
        wiki += "|}\n\n"

    if items['weapons']:
        wiki += "== Weapons ==\n"
        wiki += (
            '{| class="wikitable sortable" style="width:100%"\n'
            '! Name !! Wield !! Damage !! Avg !! Stat 1 !! Stat 2 !! Stat 3 !! Special !! Align !! Disenchant !! Loaded by\n'
        )
        for r in sorted(items['weapons'], key=lambda x: x.get('Name', '')):
            wiki += (
                f"|-\n"
                f"| {wikilink(r.get('Name',''))} "
                f"|| {wdash(r.get('Wield',''))} "
                f"|| {wdash(r.get('Damage',''))} "
                f"|| {wdash(r.get('Avg',''))} "
                f"|| {wdash(r.get('Stat1',''))} "
                f"|| {wdash(r.get('Stat2',''))} "
                f"|| {wdash(r.get('Stat3',''))} "
                f"|| {wdash(r.get('Special',''))} "
                f"|| {wdash(r.get('Alignment',''))} "
                f"|| {wdash(r.get('Disenchant',''))} "
                f"|| {wikilink_list(r.get('Loads',''))}\n"
            )
        wiki += "|}\n\n"

    if items.get('chests'):
        wiki += "== Chests ==\n"
        wiki += (
            '{| class="wikitable sortable" style="width:100%"\n'
            '! Chest !! Contents\n'
        )
        for chest_name in sorted(items['chests']):
            contents = items['chests'][chest_name]
            contents_str = ', '.join(f'[[{_strip_article(i)}]]' for i in sorted(contents) if i)
            wiki += f"|-\n| {wikilink(chest_name)} || {contents_str}\n"
        wiki += "|}\n\n"

    return wiki


def run_wiki_generator():
    print("\nWiki Generator")
    print("=" * 40)

    armor_path   = os.path.join(SCRIPT_DIR, 'armor.csv')
    weapons_path = os.path.join(SCRIPT_DIR, 'weapons.csv')
    npc_path     = os.path.join(SCRIPT_DIR, 'npcs_considered.csv')

    def _read(path):
        if not os.path.exists(path):
            return []
        with open(path, newline='', encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))

    armor_rows   = _read(armor_path)
    weapon_rows  = _read(weapons_path)
    npc_rows     = _read(npc_path)
    chest_rows   = _read(os.path.join(SCRIPT_DIR, 'chests.csv'))

    location_data = build_location_data(armor_rows, weapon_rows, npc_rows, chest_rows)
    locations     = sorted(location_data.keys())
    print(f"  {len(locations)} unique locations")

    os.makedirs(WIKI_OUT_DIR, exist_ok=True)
    for loc in locations:
        items = location_data[loc]
        wiki  = generate_location_page(loc, items)
        fname = os.path.join(WIKI_OUT_DIR, safe_filename(loc))
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(wiki)

    print(f"  {len(locations)} pages written -> {WIKI_OUT_DIR}/")


# =====================================================================
# MAIN PIPELINE
# =====================================================================

def main():
    import argparse
    ap = argparse.ArgumentParser(description='Ishar MUD log parser + load linker + wiki generator')
    ap.add_argument('logs', nargs='*', help='Log file(s) to parse')
    ap.add_argument('--level', type=int, default=None,
                    help='Override player level (use when log has no sc command)')
    args = ap.parse_args()

    if args.logs:
        log_files = args.logs
    else:
        log_files = [
            os.path.join(SCRIPT_DIR, f)
            for f in os.listdir(SCRIPT_DIR)
            if f.endswith('.txt') and os.path.isfile(os.path.join(SCRIPT_DIR, f))
        ]

    if not log_files:
        print("No log files found.")
        return

    # ── Phase 1: Parse logs ──────────────────────────────────────────
    print("Ishar Log Parser")
    print("=" * 40)

    parser = LogParser()
    if args.level is not None:
        parser.player_level = args.level
        print(f"  Player level override: {args.level}")
    for lf in log_files:
        parser.parse_file(lf)

    # Write NPCs
    npc_path = os.path.join(SCRIPT_DIR, 'npcs_considered.csv')
    npc_rows = []
    for key, rec in parser.npcs.items():
        npc_rows.append({
            'Name':     rec.get('Name', key[0] if isinstance(key, tuple) else key),
            'Level':    rec.get('Level', ''),
            'Class':    rec.get('Class', ''),
            'Race':     rec.get('Race', ''),
            'Location': rec.get('Location', ''),
            'Loads':    rec.get('Loads', ''),
        })
    existing_npc_rows = []
    if os.path.exists(npc_path):
        with open(npc_path, newline='', encoding='utf-8-sig') as f:
            existing_npc_rows = list(csv.DictReader(f))
    seen_npc_pairs = {(r.get('Name',''), r.get('Location','')): r for r in existing_npc_rows}
    added = updated = 0
    for row in npc_rows:
        pair = (row['Name'], row['Location'])
        if pair not in seen_npc_pairs:
            seen_npc_pairs[pair] = row
            added += 1
        else:
            ex = seen_npc_pairs[pair]
            ex_lvl  = str(ex.get('Level', ''))
            new_lvl = str(row.get('Level', ''))
            if is_estimate_level(ex_lvl) and not is_estimate_level(new_lvl):
                ex['Level'] = new_lvl
                updated += 1
            ex_loads  = ex.get('Loads', '')
            new_loads = row.get('Loads', '')
            if new_loads and new_loads != '-' and new_loads not in ex_loads:
                ex['Loads'] = (ex_loads + ', ' + new_loads).strip(', ') if ex_loads else new_loads
                updated += 1
    write_csv(npc_path, list(seen_npc_pairs.values()), NPC_FIELDS)
    print(f"\nNPCs:    {added} added, {updated} updated -> {npc_path}")

    # Write descriptions
    desc_path = os.path.join(SCRIPT_DIR, 'npcs_descriptions.csv')
    existing_desc = load_existing_csv(desc_path, DESC_FIELDS)
    new_descs = {k: {'Name': k, 'Description': v} for k, v in parser.descriptions.items() if k not in existing_desc}
    all_desc = dict(existing_desc)
    all_desc.update(new_descs)
    write_csv(desc_path, list(all_desc.values()), DESC_FIELDS)
    print(f"Descs:   {len(new_descs)} added -> {desc_path}")

    # Write armor
    armor_path = os.path.join(SCRIPT_DIR, 'armor.csv')
    a, u = merge_and_write(armor_path, parser.armor, ARMOR_FIELDS)
    print(f"Armor:   {a} added, {u} updated -> {armor_path}")

    # Write weapons
    weapons_path = os.path.join(SCRIPT_DIR, 'weapons.csv')
    a, u = merge_and_write(weapons_path, parser.weapons, WEAPON_FIELDS)
    print(f"Weapons: {a} added, {u} updated -> {weapons_path}")

    # Write chests — one row per (Chest, Location), Contents collapsed into single column
    chest_path = os.path.join(SCRIPT_DIR, 'chests.csv')
    # Load existing and build (Chest, Location) -> set of items
    existing_chest_map = {}  # (chest, loc) -> set of items
    if os.path.exists(chest_path):
        with open(chest_path, newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                key = (row.get('Chest',''), row.get('Location',''))
                # Support both old 'Item' column and new 'Contents' column
                raw = row.get('Contents', '') or row.get('Item', '')
                contents = {i.strip() for i in raw.split(',') if i.strip()}
                existing_chest_map.setdefault(key, set()).update(contents)
    new_chest_count = 0
    for row in parser.chests:
        key = (row['Chest'], row['Location'])
        item = row['Item']
        if key not in existing_chest_map:
            existing_chest_map[key] = set()
        if item not in existing_chest_map[key]:
            existing_chest_map[key].add(item)
            new_chest_count += 1
    # Write collapsed rows
    chest_rows_out = [
        {'Chest': k[0], 'Location': k[1], 'Contents': ', '.join(sorted(items))}
        for k, items in sorted(existing_chest_map.items())
    ]
    with open(chest_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CHEST_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(chest_rows_out)
    print(f"Chests: {new_chest_count} new item(s) across {len(chest_rows_out)} chest(s) -> {chest_path}")

    # ── Phase 2: Link loads ──────────────────────────────────────────
    run_link_loads()

    # ── Phase 3: Generate wiki pages ─────────────────────────────────
    run_wiki_generator()

    # ── Phase 4: Move parsed logs to parsed/ subfolder ───────────────
    parsed_dir = os.path.join(SCRIPT_DIR, 'parsed')
    os.makedirs(parsed_dir, exist_ok=True)
    moved = []
    for lf in log_files:
        dest = os.path.join(parsed_dir, os.path.basename(lf))
        # If a file with the same name already exists in parsed/, add a suffix
        if os.path.exists(dest):
            base, ext = os.path.splitext(os.path.basename(lf))
            i = 1
            while os.path.exists(dest):
                dest = os.path.join(parsed_dir, f"{base}_{i}{ext}")
                i += 1
        os.rename(lf, dest)
        moved.append(os.path.basename(dest))

    print(f"\nMoved {len(moved)} log(s) to {parsed_dir}/")
    for name in moved:
        print(f"  {name}")

    print("\nAll done.")


if __name__ == '__main__':
    main()

