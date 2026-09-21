class Ledger:
    def __init__(self):
        self.balances = {}
        self.applied = set()
        self.hist = {}

    def open(self, account, balance=0):
        if account in self.balances:
            raise ValueError("exists")
        self.balances[account] = balance
        self.hist[account] = []

    def transfer(self, txid, src, dst, amount):
        if txid in self.applied:
            return False
        if amount <= 0 or src == dst or src not in self.balances or dst not in self.balances:
            raise ValueError("invalid")
        if self.balances[src] < amount:
            raise ValueError("insufficient funds")
        self.balances[src] -= amount
        self.balances[dst] += amount
        self.hist[src].append((txid, -amount))
        self.hist[dst].append((txid, amount))
        self.applied.add(txid)
        return True

    def balance(self, account):
        return self.balances[account]

    def history(self, account):
        return list(self.hist[account])
