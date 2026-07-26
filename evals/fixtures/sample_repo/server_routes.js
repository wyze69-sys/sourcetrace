const express = require('express');
const router = express.Router();

router.get('/summary', (req, res) => {
  res.json({ total: 0 });
});

const app = express();
app.use('/api/v1/reports', router);
