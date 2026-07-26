const express = require('express');
const metricsRouter = express.Router();

function readTotals(req, res) {
  res.json({ total: 0 });
}

metricsRouter.get('/totals', readTotals);

const app = express();
app.use('/api/v1/metrics', metricsRouter);

app.post('/api/v1/feedback', (req, res) => {
  res.status(201).end();
});
