import { ClobClient } from "@polymarket/clob-client";
import { getClobClient, isConfigured } from "./auth";
import {
  OrderRequest,
  OrderResponse,
  PriceResponse,
  BookResponse,
  PositionResponse,
  BalanceResponse,
} from "../types";

export async function getPrice(tokenId: string): Promise<PriceResponse> {
  const client = await getClobClient();
  const book = await client.getOrderBook(tokenId);

  const bestBid =
    book.bids && book.bids.length > 0 ? parseFloat(book.bids[0].price) : 0;
  const bestAsk =
    book.asks && book.asks.length > 0 ? parseFloat(book.asks[0].price) : 1;
  const midpoint = bestBid > 0 && bestAsk < 1 ? (bestBid + bestAsk) / 2 : 0.5;

  return {
    token_id: tokenId,
    bid: bestBid,
    ask: bestAsk,
    midpoint,
    last: midpoint,
    timestamp: Date.now(),
  };
}

export async function getBook(tokenId: string): Promise<BookResponse> {
  const client = await getClobClient();
  const book = await client.getOrderBook(tokenId);

  return {
    token_id: tokenId,
    bids: (book.bids || []).map((b: any) => ({
      price: parseFloat(b.price),
      size: parseFloat(b.size),
    })),
    asks: (book.asks || []).map((a: any) => ({
      price: parseFloat(a.price),
      size: parseFloat(a.size),
    })),
    timestamp: Date.now(),
  };
}

export async function placeOrder(
  req: OrderRequest
): Promise<OrderResponse> {
  const client = await getClobClient();

  const order = await client.createAndPostOrder({
    tokenID: req.token_id,
    price: req.price,
    side: req.side === "BUY" ? 0 : 1, // 0 = BUY, 1 = SELL
    size: req.size,
  });

  return {
    order_id: (order as any).id || "unknown",
    status: (order as any).status || "placed",
    token_id: req.token_id,
    price: req.price,
    size: req.size,
    side: req.side,
    timestamp: Date.now(),
  };
}

export async function cancelOrder(orderId: string): Promise<boolean> {
  const client = await getClobClient();
  await client.cancelOrder({ id: orderId } as any);
  return true;
}

export async function getPositions(): Promise<PositionResponse[]> {
  // Note: Position tracking uses the Polymarket API
  // The CLOB client doesn't directly expose positions;
  // we track positions in the Python layer
  return [];
}

export async function getBalance(): Promise<BalanceResponse> {
  // Balance check requires on-chain query
  // For paper mode, this is tracked in Python
  return {
    usdc: 0,
    allowance: 0,
  };
}

export function checkConnection(): boolean {
  return isConfigured();
}
