import { ethers } from "ethers";
import { ClobClient } from "@polymarket/clob-client";
import dotenv from "dotenv";
import path from "path";

// Load env from config/.env
dotenv.config({ path: path.resolve(__dirname, "../../../config/.env") });

const CLOB_API_URL = "https://clob.polymarket.com";
const CHAIN_ID = 137;

export interface AuthConfig {
  privateKey: string;
  clobApiUrl: string;
  chainId: number;
}

export function getAuthConfig(): AuthConfig {
  const privateKey = process.env.POLY_PRIVATE_KEY;
  if (!privateKey) {
    throw new Error("POLY_PRIVATE_KEY not set in environment");
  }

  return {
    privateKey: privateKey.startsWith("0x") ? privateKey : `0x${privateKey}`,
    clobApiUrl: CLOB_API_URL,
    chainId: CHAIN_ID,
  };
}

let clientInstance: ClobClient | null = null;

export async function getClobClient(): Promise<ClobClient> {
  if (clientInstance) {
    return clientInstance;
  }

  const config = getAuthConfig();
  const wallet = new ethers.Wallet(config.privateKey);

  const client = new ClobClient(
    config.clobApiUrl,
    config.chainId,
    wallet as any
  );

  // Derive API credentials
  await client.createOrDeriveApiKey();

  clientInstance = client;
  return client;
}

export function getWalletAddress(): string {
  const config = getAuthConfig();
  const wallet = new ethers.Wallet(config.privateKey);
  return wallet.address;
}

export function isConfigured(): boolean {
  return !!process.env.POLY_PRIVATE_KEY;
}
