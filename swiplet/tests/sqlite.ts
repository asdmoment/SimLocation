// node:sqlite behind the part of expo-sqlite that src/store.ts uses.
import { DatabaseSync } from "node:sqlite";
import type { Database } from "../src/store";

type Value = string | number | null;

export function open(path: string): Database & { close(): void } {
  const db = new DatabaseSync(path);
  return {
    execSync(source) {
      db.exec(source);
    },
    runSync: (source, ...params) => ({ lastInsertRowId: Number(db.prepare(source).run(...params).lastInsertRowid) }),
    // Plain objects, as expo-sqlite returns; node:sqlite rows have no prototype.
    getFirstSync<T>(source: string, ...params: Value[]) {
      const row = db.prepare(source).get(...params);
      return (row ? { ...row } : null) as T | null;
    },
    getAllSync: <T>(source: string, ...params: Value[]) => db.prepare(source).all(...params).map((row) => ({ ...row })) as T[],
    withTransactionSync(task) {
      db.exec("BEGIN");
      try {
        task();
        db.exec("COMMIT");
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
    close: () => db.close(),
  };
}
