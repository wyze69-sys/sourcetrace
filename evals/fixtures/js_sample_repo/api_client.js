import axios from 'axios';
import { formatTotals as summarize } from './format_utils';

export async function loadDashboardTotals() {
  const res = await axios.get('/api/v1/metrics/totals');
  return summarize(res.data);
}

export async function submitFeedback(payload) {
  return fetch('/api/v1/feedback', { method: 'POST', body: JSON.stringify(payload) });
}

export async function pollOrphanStatus() {
  const res = await fetch('/api/v1/orphan/status');
  return res.json();
}
