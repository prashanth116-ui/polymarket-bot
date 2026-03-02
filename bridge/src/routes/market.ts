import { Router, Request, Response } from "express";
import { getPrice, getBook } from "../services/clob";

const router = Router();

router.get("/price/:token_id", async (req: Request, res: Response) => {
  try {
    const tokenId = req.params.token_id;
    const price = await getPrice(tokenId);
    res.json(price);
  } catch (err: any) {
    console.error("Price fetch error:", err.message);
    res.status(500).json({ error: "Failed to fetch price", details: err.message });
  }
});

router.get("/book/:token_id", async (req: Request, res: Response) => {
  try {
    const tokenId = req.params.token_id;
    const book = await getBook(tokenId);
    res.json(book);
  } catch (err: any) {
    console.error("Book fetch error:", err.message);
    res.status(500).json({ error: "Failed to fetch order book", details: err.message });
  }
});

export default router;
