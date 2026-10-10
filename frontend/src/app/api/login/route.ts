import { authenticate, readJson, str } from "@/lib/auth";
import { validateLogin } from "@/lib/auth-rules";

export async function POST(request: Request) {
  const body = await readJson(request);
  const email = str(body.email).trim().toLowerCase();
  const password = str(body.password);

  const error = validateLogin(email, password);
  if (error) {
    return Response.json({ error }, { status: 400 });
  }

  return authenticate("/api/auth/login", { email, password }, "Login failed");
}
