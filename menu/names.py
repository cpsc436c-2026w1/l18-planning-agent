"""Names of the weekly-menu resources. One place, read by the publisher, the set-up and the tests."""
import os

TOPIC = "l18-menu-ready"              # SNS: "a menu was approved"
QUEUE = "l18-menu-render"             # SQS: work for the email function
DEAD_LETTER_QUEUE = "l18-menu-render-dlq"
SENT_TABLE = "l18-menu-sent"          # DynamoDB: one item per request_id, written before the email is sent
FUNCTION = "l18-menu-email"
ADDRESS_PARAM = "/probe436c/l18-menu-email-to"    # SSM: the address the menu is sent from and to
MAX_RECEIVES = 3                      # a message received 3 times without success moves to the dead-letter queue
FUNCTION_TIMEOUT_S = 10
VISIBILITY_TIMEOUT_S = 6 * FUNCTION_TIMEOUT_S     # AWS: at least six times the function's timeout
REGION = os.environ.get("AWS_REGION", "ca-central-1")
