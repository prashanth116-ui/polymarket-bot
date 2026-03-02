import { Router, Request, Response } from "express";
import { placeOrder, cancelOrder } from "../services/clob";
import { OrderRequest } from "../types";

const router = Router();

router.post("/", async (req: Request, res: Response) => {
  try {
    const orderReq: OrderRequest = {
      token_id: req.body.token_id,
      price: req.body.price,
      size: req.body.size,
      side: req.body.side,
      type: req.body.type || "GTC",
      expiration: req.body.expiration,
    };

    if (!orderReq.token_id || !orderReq.price || !orderReq.size || !orderReq.side) {
      res.status(400).json({ error: "Missing required fields: token_id, price, size, side" });
      return;
    }

    if (orderReq.price <= 0 || orderReq.price >= 1) {
      res.status(400).json({ error: "Price must be between 0 and 1 (exclusive)" });
      return;
    }

    const result = await placeOrder(orderReq);
    res.json(result);
  } catch (err: any) {
    console.error("Order placement error:", err.message);
    res.status(500).json({ error: "Failed to place order", details: err.message });
  }
});

router.delete("/:id", async (req: Request, res: Response) => {
  try {
    const orderId = req.params.id;
    if (!orderId) {
      res.status(400).json({ error: "Order ID required" });
      return;
    }

    await cancelOrder(orderId);
    res.json({ status: "cancelled", order_id: orderId });
  } catch (err: any) {
    console.error("Order cancel error:", err.message);
    res.status(500).json({ error: "Failed to cancel order", details: err.message });
  }
});

export default router;
