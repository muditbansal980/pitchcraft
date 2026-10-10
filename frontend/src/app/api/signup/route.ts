import { authenticate, readJson, str } from "@/lib/auth";
import { validateSignup } from "@/lib/auth-rules";

export async function POST(request: Request) {
  const body = await readJson(request);
  const username = str(body.username).trim();
  const email = str(body.email).trim().toLowerCase();
  const password = str(body.password);

  const error = validateSignup(username, email, password);
  if (error) {
    return Response.json({ error }, { status: 400 });
  }

  return authenticate("/api/auth/signup", { username, email, password }, "Sign up failed");
}
