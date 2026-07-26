export function syncAll(records) {
  return records.map((record) => persistRecord(record));
}
