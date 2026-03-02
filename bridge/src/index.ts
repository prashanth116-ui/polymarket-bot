import express from "express";
import orderRoutes from "./routes/orders";
import marketRoutes from "./routes/market";
import positionRoutes from "./routes/positions";
import { checkConnection } from "./services/clob";

const app = express();
const PORT = parseInt(process.env.BRIDGE_PORT || "8420", 10);

app.use(express.json());

// Health check
app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    mode: process.env.POLY_PRIVATE_KEY ? "configured" : "unconfigured",
    connected: checkConnection(),
    timestamp: Date.now(),
  });
});

// Routes
app.use("/order", orderRoutes);
app.use("/market", marketRoutes);
app.use("/", positionRoutes);

app.listen(PORT, "127.0.0.1", () => {
  console.log(`Polymarket bridge running on http://127.0.0.1:${PORT}`);
  console.log(`Wallet configured: ${checkConnection()}`);
});
