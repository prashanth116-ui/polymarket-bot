export interface OrderRequest {
  token_id: string;
  price: number;
  size: number;
  side: "BUY" | "SELL";
  type: "LMT" | "MKT" | "GTC" | "GTD";
  expiration?: number; // Unix timestamp for GTD orders
}

export interface OrderResponse {
  order_id: string;
  status: string;
  token_id: string;
  price: number;
  size: number;
  side: string;
  timestamp: number;
}

export interface PriceResponse {
  token_id: string;
  bid: number;
  ask: number;
  midpoint: number;
  last: number;
  timestamp: number;
}

export interface BookLevel {
  price: number;
  size: number;
}

export interface BookResponse {
  token_id: string;
  bids: BookLevel[];
  asks: BookLevel[];
  timestamp: number;
}

export interface PositionResponse {
  market_id: string;
  token_id: string;
  outcome: string;
  size: number;
  avg_price: number;
  current_price: number;
  unrealized_pnl: number;
}

export interface BalanceResponse {
  usdc: number;
  allowance: number;
}

export interface HealthResponse {
  ok: boolean;
  mode: string;
  connected: boolean;
  timestamp: number;
}

export interface ErrorResponse {
  error: string;
  details?: string;
}
