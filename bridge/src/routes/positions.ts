import { Router, Request, Response } from "express";
import { getPositions, getBalance } from "../services/clob";

const router = Router();

router.get("/positions", async (_req: Request, res: Response) => {
  try {
    const positions = await getPositions();
    res.json(positions);
  } catch (err: any) {
    console.error("Positions fetch error:", err.message);
    res.status(500).json({ error: "Failed to fetch positions", details: err.message });
  }
});

router.get("/balance", async (_req: Request, res: Response) => {
  try {
    const balance = await getBalance();
    res.json(balance);
  } catch (err: any) {
    console.error("Balance fetch error:", err.message);
    res.status(500).json({ error: "Failed to fetch balance", details: err.message });
  }
});

export default router;
