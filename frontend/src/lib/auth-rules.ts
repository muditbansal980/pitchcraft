// Shared by the auth forms (client) and the /api/login and /api/signup routes (server).
export const PASSWORD_MIN_LENGTH = 8;
// Backend rejects longer passwords (SignupRequest/LoginRequest max_length).
export const PASSWORD_MAX_LENGTH = 128;
export const USERNAME_MIN_LENGTH = 3;
export const USERNAME_MAX_LENGTH = 30;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateLogin(email: string, password: string): string | null {
  if (!EMAIL_RE.test(email)) return "Enter a valid email address";
  if (!password) return "Password is required";
  if (password.length > PASSWORD_MAX_LENGTH) return "Invalid email or password";
  return null;
}

export function validateSignup(username: string, email: string, password: string): string | null {
  if (username.length < USERNAME_MIN_LENGTH || username.length > USERNAME_MAX_LENGTH) {
    return `Username must be ${USERNAME_MIN_LENGTH}–${USERNAME_MAX_LENGTH} characters`;
  }
  if (!EMAIL_RE.test(email)) return "Enter a valid email address";
  if (password.length < PASSWORD_MIN_LENGTH) {
    return `Password must be at least ${PASSWORD_MIN_LENGTH} characters`;
  }
  if (password.length > PASSWORD_MAX_LENGTH) {
    return `Password must be at most ${PASSWORD_MAX_LENGTH} characters`;
  }
  return null;
}
