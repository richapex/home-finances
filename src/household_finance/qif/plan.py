"""Turn a parsed QIF file into a database-independent double-entry import plan.

The plan contains every account/category, payee and tag to create and every
transaction as a set of splits that sum to zero, using the schema's sign
convention (positive = debit). It is what the database writer will insert,
and what the report command prints for checking against Quicken.

Transfers
---------
Quicken exports a transfer in both accounts' registers. Each register line
that names another account (``L[Savings]`` or ``S[Savings]``) is matched to
a line in that account's register that names this account, with the opposite
amount and the same date. Matched register transactions are merged into one
transaction. The merge takes, from every register transaction in the group,
its own account split plus all of its lines except the matched transfer
lines (those are represented by the other side's account split), so the
result always balances.

If no same-date partner exists, a partner within ``date_window_days`` is
accepted and reported. A transfer line left without a partner still posts to
the named account, so the register it came from keeps the right balance, and
is reported for review.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .parser import QifFile, QifTransaction
from .values import name_key

OPENING_BALANCES = "Opening Balances"
UNCATEGORIZED = "Uncategorized"

# Column sizes in db/schema.sql
MAX_ACCOUNT_NAME = 100
MAX_PAYEE = 150
MAX_MEMO = 255
MAX_CHECK_NUMBER = 20
MAX_TAG = 100
MAX_SOURCE_REF = 255

ZERO = Decimal("0.00")


@dataclass
class PlannedAccount:
    path: str  # full colon-separated path, e.g. "Auto:Fuel"
    kind: str  # asset | liability | income | expense | equity
    subtype: str
    origin: str  # system | qif_account | qif_category | referenced
    description: str = ""
    tax_line: str | None = None
    source_ref: str | None = None  # original QIF name
    history_imported: bool = True  # False: no register in the file

    @property
    def name(self) -> str:
        return self.path.rsplit(":", 1)[-1]

    @property
    def parent_path(self) -> str | None:
        return self.path.rsplit(":", 1)[0] if ":" in self.path else None

    @property
    def is_category(self) -> bool:
        return self.kind in ("income", "expense")


@dataclass
class PlannedSplit:
    account: str  # PlannedAccount.path
    amount: Decimal
    cleared: str
    memo: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class PlannedTransaction:
    date: date
    payee: str | None
    check_number: str | None
    memo: str
    splits: list[PlannedSplit]
    source_refs: list[str]
    notes: list[str] = field(default_factory=list)

    @property
    def source_ref(self) -> str:
        return "; ".join(self.source_refs)[:MAX_SOURCE_REF]

    @property
    def total(self) -> Decimal:
        return sum((s.amount for s in self.splits), ZERO)


@dataclass
class TransferIssue:
    status: str  # 'date_mismatch' | 'unmatched'
    account: str
    other_account: str
    date: date
    amount: Decimal  # from `account`'s point of view
    payee: str | None
    source_ref: str
    other_date: date | None = None
    other_source_ref: str | None = None


@dataclass
class ImportPlan:
    accounts: dict[str, PlannedAccount] = field(default_factory=dict)  # by name_key(path)
    payees: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)  # name -> description
    transactions: list[PlannedTransaction] = field(default_factory=list)
    transfer_issues: list[TransferIssue] = field(default_factory=list)
    matched_transfers: int = 0
    renames: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    register_transactions: int = 0
    skipped_investment: Counter[str] = field(default_factory=Counter)
    skipped_records: Counter[str] = field(default_factory=Counter)

    def account(self, path: str) -> PlannedAccount | None:
        return self.accounts.get(path_key(path))


def path_key(path: str) -> str:
    return ":".join(name_key(seg) for seg in path.split(":"))


def classify_account(qif_type: str, name: str) -> tuple[str, str]:
    """Map a QIF account type to (kind, subtype). Bank, liability and asset
    subtypes are guessed from the name and can be changed after import."""
    t = qif_type.strip().casefold()
    n = name.casefold()
    if t == "bank":
        return "asset", "savings" if ("saving" in n or "money market" in n) else "checking"
    if t == "cash":
        return "asset", "cash"
    if t == "ccard":
        return "liability", "credit_card"
    if t == "oth a":
        if any(w in n for w in ("house", "home", "residence", "property", "condo")):
            return "asset", "property"
        return "asset", "other_asset"
    if t == "oth l":
        if "mortgage" in n:
            return "liability", "mortgage"
        if "heloc" in n or "line of credit" in n:
            return "liability", "line_of_credit"
        if "loan" in n:
            return "liability", "loan"
        return "liability", "other_liability"
    if any(k in t for k in ("invst", "port", "401", "403", "mutual")):
        if any(w in t or w in n for w in ("401", "403", "457", "ira", "roth", "retire")):
            return "asset", "retirement"
        return "asset", "investment"
    return "asset", "other_asset"


def build_plan(qif: QifFile, date_window_days: int = 7) -> ImportPlan:
    return _Builder(qif, date_window_days).run()


@dataclass
class _Line:
    """A QIF line with its target resolved to a planned account."""

    account: str  # path of the account the line posts to
    amount: Decimal  # register perspective
    memo: str
    tag: str | None
    transfer_to: str | None  # key of another account with an imported register


class _Builder:
    def __init__(self, qif: QifFile, date_window_days: int) -> None:
        self.qif = qif
        self.window = date_window_days
        self.plan = ImportPlan(
            register_transactions=len(qif.transactions),
            skipped_investment=Counter(qif.skipped_investment),
            skipped_records=Counter(qif.skipped_records),
        )
        self.plan.warnings.extend(str(w) for w in qif.warnings)
        self.account_paths: dict[str, str] = {}  # name_key(QIF account) -> path
        self.category_paths: dict[str, str] = {}  # path_key(QIF category) -> path
        self.tag_names: dict[str, str] = {}  # name_key -> canonical tag
        self.payee_names: dict[str, str] = {}  # name_key -> canonical payee
        self._issue_refs: list[tuple[int, str]] = []  # (register txn index, note)

    def warn(self, message: str) -> None:
        self.plan.warnings.append(message)

    def fit(self, text: str | None, limit: int, what: str) -> str | None:
        if text is not None and len(text) > limit:
            self.warn(f"{what} {text!r} truncated to {limit} characters")
            return text[:limit]
        return text

    def add_account(self, acct: PlannedAccount) -> PlannedAccount:
        self.plan.accounts[path_key(acct.path)] = acct
        return acct

    # -- steps ------------------------------------------------------------------

    def run(self) -> ImportPlan:
        self.add_account(PlannedAccount(OPENING_BALANCES, "equity", "equity", "system"))
        self.add_account(PlannedAccount(UNCATEGORIZED, "expense", "category", "system"))
        self.build_accounts()
        self.build_categories()
        self.build_tags()
        self.build_transactions()
        self.plan.payees = sorted(self.payee_names.values(), key=name_key)
        return self.plan

    def build_accounts(self) -> None:
        with_register = {name_key(t.account) for t in self.qif.transactions}
        for key, qa in self.qif.accounts.items():
            self.add_real_account(qa.name, qa.qif_type, qa.description,
                                  origin="qif_account",
                                  history=key in with_register and not qa.is_investment)
            if not qa.is_investment and key not in with_register:
                self.warn(f"account {qa.name!r} has no transactions in the file")
        # Accounts named in [ ] but not exported.
        for t in self.qif.transactions:
            for ln in t.lines:
                if ln.ref.is_transfer and name_key(ln.ref.name) not in self.account_paths:
                    self.add_real_account(ln.ref.name, "", "", origin="referenced",
                                          history=False)
                    self.warn(f"account {ln.ref.name!r} is a transfer target but is "
                              f"not in the file (first used at {t.source_ref}); "
                              "created as an other asset")

    def add_real_account(self, qif_name: str, qif_type: str, description: str,
                         origin: str, history: bool) -> None:
        name = qif_name.replace(":", "-").strip()
        if name != qif_name:
            self.plan.renames.append(f"account {qif_name!r} -> {name!r} "
                                     "(':' is not allowed in names)")
        name = self.fit(name, MAX_ACCOUNT_NAME, "account name")
        name = self.unique_name(name)
        kind, subtype = classify_account(qif_type, qif_name)
        self.add_account(PlannedAccount(
            path=name, kind=kind, subtype=subtype, origin=origin,
            description=description, source_ref=qif_name, history_imported=history,
        ))
        self.account_paths[name_key(qif_name)] = name

    def unique_name(self, name: str, suffix: str = " (account)") -> str:
        """Top-level name not already used by another account or category."""
        candidate, n = name, 1
        while path_key(candidate) in self.plan.accounts:
            n += 1
            candidate = f"{name}{suffix}" if (suffix and n == 2) else f"{name}{suffix} {n}"
        if candidate != name:
            self.plan.renames.append(f"{name!r} -> {candidate!r} (name already used)")
        return candidate

    def build_categories(self) -> None:
        # Listed categories first (they carry income/expense and tax info),
        # then categories used in transactions but missing from the list.
        listed = {path_key(c.name): c for c in self.qif.categories}
        used_sign: dict[str, Decimal] = defaultdict(lambda: ZERO)
        used_names: dict[str, str] = {}
        for t in self.qif.transactions:
            for ln in t.lines:
                if not ln.ref.is_transfer and ln.ref.name:
                    k = path_key(ln.ref.name)
                    used_sign[k] += ln.amount
                    used_names.setdefault(k, ln.ref.name)

        # Top-level category names that collide with a real account.
        top_renames: dict[str, str] = {}
        tops = {name_key(n.split(":")[0].strip()): n.split(":")[0].strip()
                for n in [c.name for c in self.qif.categories] + list(used_names.values())}
        for top_key, top in tops.items():
            existing = self.plan.accounts.get(top_key)
            if existing is not None and not existing.is_category:
                new = self.unique_name(f"{top} (category)", suffix="")
                top_renames[top_key] = new
                self.plan.renames.append(
                    f"category {top!r} -> {new!r} (an account has the same name)")

        for key, cat in listed.items():
            self.add_category(cat.name, "income" if cat.is_income else "expense",
                              top_renames, description=cat.description,
                              tax_line=cat.tax_line or ("Tax related" if cat.tax_related else None))
        for key, name in used_names.items():
            if key in listed:
                continue
            kind = "income" if used_sign[key] > 0 else "expense"
            path = self.add_category(name, kind, top_renames)
            self.warn(f"category {name!r} is used but not in the category list; "
                      f"created as {self.plan.account(path).kind}")

    def add_category(self, qif_name: str, kind: str, top_renames: dict[str, str],
                     description: str = "", tax_line: str | None = None) -> str:
        segments = [s.strip() for s in qif_name.split(":")]
        if any(not s for s in segments):
            self.warn(f"category {qif_name!r} has an empty level; empty levels dropped")
            segments = [s for s in segments if s] or [UNCATEGORIZED]
        segments = [self.fit(s, MAX_ACCOUNT_NAME, "category name") for s in segments]
        segments[0] = top_renames.get(name_key(segments[0]), segments[0])

        path = ""
        for depth, seg in enumerate(segments):
            path = f"{path}:{seg}" if path else seg
            existing = self.plan.account(path)
            is_leaf = depth == len(segments) - 1
            if existing is None:
                parent = self.plan.account(path.rsplit(":", 1)[0]) if depth else None
                acct_kind = parent.kind if parent else kind
                if parent and parent.kind != kind and is_leaf:
                    self.warn(f"category {qif_name!r} is {kind} but its parent is "
                              f"{parent.kind}; using {parent.kind}")
                existing = self.add_account(PlannedAccount(
                    path=path, kind=acct_kind, subtype="category", origin="qif_category",
                ))
            elif is_leaf and existing.kind != kind and existing.origin == "qif_category":
                if existing.source_ref is None:
                    # Created earlier as the implicit parent of a child listed
                    # before it; the listed kind wins, for the children too.
                    existing.kind = kind
                    self.retype_children(path, kind)
                else:
                    self.warn(f"category {qif_name!r} listed as both income and expense")
            if is_leaf:
                existing.description = existing.description or description
                existing.tax_line = existing.tax_line or tax_line
                existing.source_ref = existing.source_ref or qif_name
        self.category_paths[path_key(qif_name)] = path
        return path

    def retype_children(self, path: str, kind: str) -> None:
        prefix = path_key(path) + ":"
        for k, acct in self.plan.accounts.items():
            if k.startswith(prefix) and acct.kind != kind:
                acct.kind = kind

    def build_tags(self) -> None:
        for cls in self.qif.classes:
            name = self.tag(cls.name)
            self.plan.tags.setdefault(name, cls.description)
        for t in self.qif.transactions:
            for ln in t.lines:
                if ln.ref.tag:
                    self.plan.tags.setdefault(self.tag(ln.ref.tag), "")

    def tag(self, name: str) -> str:
        key = name_key(name)
        if key not in self.tag_names:
            self.tag_names[key] = self.fit(name, MAX_TAG, "tag")
        return self.tag_names[key]

    def payee(self, name: str | None) -> str | None:
        if not name:
            return None
        key = name_key(name)
        if key not in self.payee_names:
            self.payee_names[key] = self.fit(name, MAX_PAYEE, "payee")
        return self.payee_names[key]

    # -- transactions -------------------------------------------------------------

    def resolve_lines(self, t: QifTransaction) -> list[_Line]:
        own_key = name_key(t.account)
        lines = []
        for ln in t.lines:
            transfer_to = None
            if ln.ref.is_transfer:
                target_key = name_key(ln.ref.name)
                if target_key == own_key:
                    account = OPENING_BALANCES  # Quicken's opening balance record
                else:
                    account = self.account_paths[target_key]
                    if self.plan.account(account).history_imported:
                        transfer_to = target_key
            elif ln.ref.name:
                account = self.category_paths[path_key(ln.ref.name)]
            else:
                account = UNCATEGORIZED
            tag = self.tag(ln.ref.tag) if ln.ref.tag else None
            lines.append(_Line(account, ln.amount, ln.memo, tag, transfer_to))
        return lines

    def build_transactions(self) -> None:
        txns = self.qif.transactions
        lines = [self.resolve_lines(t) for t in txns]
        partner = self.match_transfers(txns, lines)

        # Union-find over register transactions joined by matched transfers.
        parent = list(range(len(txns)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for (ti, _), (tj, _) in partner.items():
            ri, rj = find(ti), find(tj)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(len(txns)):
            groups[find(i)].append(i)

        issue_by_txn: dict[int, list[str]] = defaultdict(list)
        for i, ref in self._issue_refs:
            issue_by_txn[i].append(ref)

        planned = []
        for members in groups.values():
            planned.append(self.merge(members, txns, lines, partner, issue_by_txn))
        planned.sort(key=lambda p: (p[0], p[1]))
        self.plan.transactions = [p[2] for p in planned]

    def match_transfers(self, txns: list[QifTransaction],
                        lines: list[list[_Line]]) -> dict[tuple[int, int], tuple[int, int]]:
        pool: dict[tuple[str, str, Decimal], list[tuple[int, int]]] = defaultdict(list)
        candidates: list[tuple[int, int]] = []
        for ti, t in enumerate(txns):
            own = name_key(t.account)
            for li, ln in enumerate(lines[ti]):
                if ln.transfer_to is not None:
                    pool[(own, ln.transfer_to, ln.amount)].append((ti, li))
                    candidates.append((ti, li))

        partner: dict[tuple[int, int], tuple[int, int]] = {}

        def find_partner(ti: int, li: int, max_days: int) -> tuple[int, int] | None:
            t, ln = txns[ti], lines[ti][li]
            best, best_days = None, None
            for cand in pool[(ln.transfer_to, name_key(t.account), -ln.amount)]:
                if cand in partner or cand[0] == ti:
                    continue
                days = abs((txns[cand[0]].date - t.date).days)
                if days <= max_days and (best_days is None or days < best_days):
                    best, best_days = cand, days
                    if days == 0:
                        break
            return best

        for max_days in (0, self.window):
            for c in candidates:
                if c in partner:
                    continue
                p = find_partner(*c, max_days)
                if p is None:
                    continue
                partner[c] = p
                partner[p] = c
                self.plan.matched_transfers += 1
                tc, tp = txns[c[0]], txns[p[0]]
                if tc.date != tp.date:
                    self.plan.transfer_issues.append(TransferIssue(
                        "date_mismatch", tc.account, tp.account, tc.date,
                        lines[c[0]][c[1]].amount, tc.payee, tc.source_ref,
                        tp.date, tp.source_ref))
                    self._issue_refs.append(
                        (c[0], f"transfer halves dated {tc.date} ({tc.account}) and "
                               f"{tp.date} ({tp.account})"))

        for c in candidates:
            if c not in partner:
                t, ln = txns[c[0]], lines[c[0]][c[1]]
                self.plan.transfer_issues.append(TransferIssue(
                    "unmatched", t.account, ln.account, t.date, ln.amount, t.payee,
                    t.source_ref))
                self._issue_refs.append(
                    (c[0], f"unmatched transfer half: {t.account} -> [{ln.account}]"))
        return partner

    def merge(self, members: list[int], txns: list[QifTransaction],
              lines: list[list[_Line]], partner: dict[tuple[int, int], tuple[int, int]],
              issue_by_txn: dict[int, list[str]]):
        primary = next((i for i in members if txns[i].is_split), members[0])
        order = [primary] + [i for i in members if i != primary]
        head = txns[primary]
        memo = self.fit(head.memo, MAX_MEMO, "memo") or ""

        # Tags on matched transfer lines go on the other side's account split.
        account_tags: dict[int, list[str]] = defaultdict(list)
        for i in members:
            for li, ln in enumerate(lines[i]):
                if ln.tag and (i, li) in partner:
                    account_tags[partner[(i, li)][0]].append(ln.tag)

        splits: list[PlannedSplit] = []
        for i in order:
            t = txns[i]
            own_memo = t.memo if (i != primary and t.memo and t.memo != head.memo) else ""
            splits.append(PlannedSplit(
                account=self.account_paths[name_key(t.account)],
                amount=t.amount, cleared=t.cleared,
                memo=self.fit(own_memo, MAX_MEMO, "memo") or "",
                tags=sorted(set(account_tags[i]), key=name_key),
            ))
            for li, ln in enumerate(lines[i]):
                if (i, li) in partner:
                    continue
                splits.append(PlannedSplit(
                    account=ln.account, amount=-ln.amount, cleared=t.cleared,
                    memo=self.fit(ln.memo, MAX_MEMO, "memo") or "",
                    tags=[ln.tag] if ln.tag else [],
                ))

        check_number = next((txns[i].check_number for i in order if txns[i].check_number), None)
        payee = next((txns[i].payee for i in order if txns[i].payee), None)
        notes = [n for i in order for n in issue_by_txn.get(i, [])]
        planned = PlannedTransaction(
            date=head.date,
            payee=self.payee(payee),
            check_number=self.fit(check_number, MAX_CHECK_NUMBER, "check number"),
            memo=memo,
            splits=splits,
            source_refs=[txns[i].source_ref for i in order],
            notes=notes,
        )
        if planned.total != ZERO:  # cannot happen unless the merge logic is wrong
            raise AssertionError(f"unbalanced transaction from {planned.source_ref}: "
                                 f"{planned.total}")
        return head.date, min(members), planned
