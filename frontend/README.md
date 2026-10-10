This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Open in Canva (local setup)

1. Create an integration at [canva.dev](https://www.canva.dev/docs/connect/) with the scope
   `design:content:write`, and add the redirect URL `http://127.0.0.1:3000/api/canva/callback`.
2. Put `CANVA_CLIENT_ID` and `CANVA_CLIENT_SECRET` in `backend/.env` (no frontend env needed).
3. **Browse the app at `http://127.0.0.1:3000`**, not `localhost:3000`. Canva only redirects to
   `127.0.0.1`, and the login cookie is per host, so starting on `localhost` would come back logged out.

Without the Canva keys, "Open in Canva" shows "Canva isn't configured"; Download PPTX still works.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
