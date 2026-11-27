# Cart total is one cent too high with a 15% discount

A cart with one book at $19.99 and a 15% discount (`Cart(percent_off=15)`) has
`total() == 1700`. 1999 × 0.85 = 1699.15 cents, so the total should be `1699`.

Discounts that come out to whole cents, such as 10% off $10.00, are correct.
