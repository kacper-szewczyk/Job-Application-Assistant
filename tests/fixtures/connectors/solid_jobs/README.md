Synthetic responses shaped like `GET https://solid.jobs/public-api/offers/IT` (checked against
the live API on 2026-10-14). Companies, offers, keys and URLs are made up.

- `it_page0.json`, `it_page1.json`: a first fetch over two pages (page size 2). Page 0 has an
  offer without `jobOfferKey`; page 1 repeats an offer from page 0, as happens when listings
  shift while paging; offer C has an unreadable `updatedAt`.
- `it_later.json`: a later, single-page fetch: A changed, B removed, C unchanged, D new.
