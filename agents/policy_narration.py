def build_customer_block(metadata: dict) -> str:
    m = metadata
    return f"""
    CUSTOMER DETAILS:
      Name: {m.get('name')}
      Policy: {m.get('policy_number')} | Plan: {m.get('plan_name')}
      Premium Due: Rs. {m.get('due_amount')} | Due Date: {m.get('due_date')}
      Policy Started: {m.get('policy_purchase_date')} | Term: {m.get('policy_term_years')} years
      Sum Assured: Rs. {m.get('sum_assured')}
      Premium Frequency: {m.get('premium_frequency')}
      Last Payment: {m.get('last_payment_date')} via {m.get('payment_method')}
      Agent: {m.get('agent_name')} | Branch: {m.get('branch')}
      Email: {m.get('email')}
    """


def build_narration_instructions() -> str:
    return """
    CURRENT STATE: Policy Narration

    YOUR JOB:
    1. Tell the user about their policy: plan name, policy number, premium, due date.
    2. Ask: Can you make the payment? By when?
    3. Answer ANY questions (policy, coverage, payment history, agent, etc).
    4. If they give a payment date -> capture_promise_to_pay.
    5. If they refuse or have a concern -> categorize_concern.
    6. If they say partial / installment / half / EMI -> transition to partial_payment.
    7. If they say call later / call back / busy -> transition to call_back.
    """
