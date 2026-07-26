import { loadDashboardTotals } from './api_client';

export function TotalsBadge({ value }) {
  return <span className="badge">{value}</span>;
}

export function DashboardView() {
  const totals = loadDashboardTotals();
  return (
    <div>
      <TotalsBadge value={totals} />
    </div>
  );
}
