Feature: Planning and bounded agents on the grocery case
  The lecture's claims about the plain-Python versions, checked offline: a stub stands in for the LLM and a
  12-product catalogue made up for the tests (tests/products.jsonl) stands in for the data.

  Background:
    Given the test catalogue in DuckDB

  Rule: Orchestrator-workers: the model lists the parts, app code runs and checks every part

    Scenario: A plan longer than 7 dishes is cut
      Given a planner that returns 9 dishes
      When the shopper asks "Plan dinners for a week under $60, no palm oil."
      Then 7 dishes are kept of the 9 planned
      And the planner model was called once
      And the worker model was called 7 times for dishes and once for the merge
      And no product in the week's list holds palm oil

    Scenario: A plan of 3 dishes runs 3 workers
      Given a planner that returns 3 dishes
      When the shopper asks "Plan dinners for three days under $60."
      Then 3 dishes are kept of the 3 planned
      And the worker model was called 3 times for dishes and once for the merge

    Scenario: A total over 60 USD is reported
      Given a planner that returns 7 dishes
      And every product in the catalogue costs 9.50 USD
      When the shopper asks "Plan dinners for a week under $60, no palm oil."
      Then the total is 66.50 USD
      And the week is reported as over the budget of 60 USD
      And the reply says "over"

    Scenario: A total within 60 USD is reported as within
      Given a planner that returns 7 dishes
      When the shopper asks "Plan dinners for a week under $60, no palm oil."
      Then the total is the sum of the catalogue prices of the picks
      And the week is reported as within the budget of 60 USD

  Rule: Bounded agent: limits, approval and the write key live outside the model

    Scenario: The write needs approval
      Given a shopper who approves
      When shopper "sam" asks "Which is cheaper, spaghetti or penne? Add the cheaper one to my list."
      Then the shopper was asked once, about "Penne rigate"
      And the list of "sam" holds 1 "Penne rigate"

    Scenario: A declined write changes nothing
      Given a shopper who declines
      When shopper "sam" asks "Which is cheaper, spaghetti or penne? Add the cheaper one to my list."
      Then the shopper was asked once, about "Penne rigate"
      And the list of "sam" is empty

    Scenario: A retried write adds the item once
      Given a shopper who approves
      And a store whose first write succeeds and then times out
      When shopper "sam" asks "Which is cheaper, spaghetti or penne? Add the cheaper one to my list."
      Then the write was sent 2 times with the same request key
      And the list of "sam" holds 1 "Penne rigate"

    Scenario: The call cap stops a runaway loop
      Given a shopper who approves
      And an LLM that asks for another search on every call
      When shopper "sam" asks "Which is cheaper, spaghetti or penne? Add the cheaper one to my list."
      Then the run is stopped by the cap of 6 tool calls
      And 6 tool calls ran
      And the list of "sam" is empty

    Scenario: The write goes to the signed-in shopper
      Given a shopper who approves
      And an LLM that asks to add product "1001" to the list of "alex"
      When shopper "sam" asks "Add spaghetti to my list."
      Then the list of "alex" is empty
      And the list of "sam" holds 1 "Spaghetti n5"
