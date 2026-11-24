"""精确线性代数核心（有理数，无浮点）。

齿轮传动的整齿模型给出的都是线性关系：

* 外啮合 / 内啮合：``ω_b = ±(z_a/z_b)·ω_a``；
* 差动座：``2·ω_cage = ω_left + ω_right``；
* 观察：``ω_s = v``（转向 + 标记通过次数合成带符号转速）。

本模块只做三件事，全部用 :class:`fractions.Fraction`：

1. 对一组线性方程求精确 RREF，区分“唯一确定 / 有自由变量 / 互相矛盾”；
2. 判断任意线性式 ``Σ a_v·x_v`` 是否被系统唯一确定（即与零空间正交）；
3. 给出特解与零空间基，供生成具体读数和把系统改写为
   :mod:`budget_solver` 快照使用。

这里刻意保持极小的独立实现：它与 ``budget_solver`` 的求解器是两套代码路径，
后者在 :mod:`gear_demo.kinematics` 里真正被调用，二者互为交叉验证。
"""

from fractions import Fraction

from budget_solver.errors import ValidationError


class LinearSystem:
    """一组 ``Σ coeff·x = rhs`` 的精确线性系统。"""

    def __init__(self, variables, equations):
        """``equations``: 可迭代的 ``(coeffs: dict[str, Fraction], rhs: Fraction)``。

        变量顺序固定为 ``variables`` 的去重排序，便于稳定复读。
        """
        self.variables = list(dict.fromkeys(variables))
        self._index = {v: i for i, v in enumerate(self.variables)}
        self.equations = []
        for coeffs, rhs in equations:
            norm = {}
            for var, coef in coeffs.items():
                if var not in self._index:
                    raise ValidationError(f"方程引用了未知变量 {var}")
                coef = Fraction(coef)
                if coef != 0:
                    norm[var] = norm.get(var, Fraction(0)) + coef
            self.equations.append((norm, Fraction(rhs)))
        self._solve()

    # ------------------------------------------------------------- RREF
    def _solve(self):
        n = len(self.variables)
        rows = []
        for coeffs, rhs in self.equations:
            row = [Fraction(0)] * (n + 1)
            for var, coef in coeffs.items():
                row[self._index[var]] += coef
            row[n] = rhs
            rows.append(row)
        pivot_cols = []
        pivot_row = 0
        for col in range(n):
            found = None
            for r in range(pivot_row, len(rows)):
                if rows[r][col] != 0:
                    found = r
                    break
            if found is None:
                continue
            rows[pivot_row], rows[found] = rows[found], rows[pivot_row]
            pivot = rows[pivot_row][col]
            rows[pivot_row] = [value / pivot for value in rows[pivot_row]]
            for r in range(len(rows)):
                if r == pivot_row or rows[r][col] == 0:
                    continue
                factor = rows[r][col]
                rows[r] = [
                    a - factor * b for a, b in zip(rows[r], rows[pivot_row])
                ]
            pivot_cols.append(col)
            pivot_row += 1
            if pivot_row == len(rows):
                break

        self._rows = rows
        self._pivot_cols = pivot_cols
        self.inconsistent_rhs = []
        for row in rows:
            if all(v == 0 for v in row[:n]) and row[n] != 0:
                self.inconsistent_rhs.append(row[n])
        self.consistent = not self.inconsistent_rhs
        free_cols = [c for c in range(n) if c not in pivot_cols]
        self.free_variables = [self.variables[c] for c in free_cols]
        self.pivot_variables = [self.variables[c] for c in pivot_cols]
        self.particular = self._particular(free_cols)
        self.nullspace = self._nullspace(free_cols)

    def _particular(self, free_cols):
        values = {v: Fraction(0) for v in self.variables}
        for row_index, col in enumerate(self._pivot_cols):
            values[self.variables[col]] = self._rows[row_index][-1]
        return values

    def _nullspace(self, free_cols):
        basis = []
        for free_col in free_cols:
            vector = {v: Fraction(0) for v in self.variables}
            vector[self.variables[free_col]] = Fraction(1)
            for row_index, col in enumerate(self._pivot_cols):
                vector[self.variables[col]] = -self._rows[row_index][free_col]
            basis.append(vector)
        return basis

    # --------------------------------------------------------- 判定工具
    def is_determined(self, form):
        """线性式 ``Σ form[v]·x_v`` 是否被系统唯一确定。

        返回 ``(True, 值)`` 或 ``(False, None)``。判据：与每条零空间基向量正交。
        """
        for vector in self.nullspace:
            total = Fraction(0)
            for var, coef in form.items():
                total += Fraction(coef) * vector.get(var, Fraction(0))
            if total != 0:
                return False, None
        value = Fraction(0)
        for var, coef in form.items():
            value += Fraction(coef) * self.particular[var]
        return True, value

    def solve_with(self, extra_equations):
        """附加约束后重新求解一个 :class:`LinearSystem`（用于构造具体读数）。"""
        return LinearSystem(
            self.variables, list(self.equations) + list(extra_equations)
        )

    def dimension(self):
        if not self.consistent:
            return None
        return len(self.variables) - len(self._pivot_cols)
