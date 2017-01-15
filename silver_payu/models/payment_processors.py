# Copyright (c) 2017 Presslabs SRL
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json

from django_fsm import TransitionNotAllowed

from django.utils import timezone
from django.dispatch import receiver

from payu.payments import TokenPayment
from payu.signals import payment_authorized, alu_token_created

from silver.models import Transaction
from silver.models.payment_processors.base import PaymentProcessorBase
from silver.models.payment_processors.mixins import TriggeredProcessorMixin

from .payment_methods import PayUPaymentMethod

from ..views import PayUTransactionView
from ..forms import PayUTransactionForm, PayUBillingForm


class PayUTriggered(PaymentProcessorBase, TriggeredProcessorMixin):
    reference = 'payu_triggered'
    form_class = PayUTransactionForm
    payment_method_class = PayUPaymentMethod
    transaction_view_class = PayUTransactionView

    _has_been_setup = False

    def get_form(self, transaction, request):
        form = PayUBillingForm(payment_method=transaction.payment_method,
                               transaction=transaction, request=request,
                               data=request.POST)

        if form.is_valid():
            if not transaction.payment_method.archived_customer:
                address = "{} {}".format(transaction.document.customer.address_1,
                                         transaction.document.customer.address_2)

                archived_customer = form.to_payu_billing()
                archived_customer['BILL_ADDRESS'] = address

                transaction.payment_method.archived_customer = archived_customer
                transaction.payment_method.save()

            form = PayUTransactionForm(payment_method=transaction.payment_method,
                                       transaction=transaction, request=request,
                                       billing_details=form.to_payu_billing())

        return form

    def refund_transaction(self, transaction, payment_method=None):
        pass

    def void_transaction(self, transaction, payment_method=None):
        pass

    def update_transaction_status(self, transaction, status):
        try:
            if status == "pending":
                transaction.process()
            elif status == "failed":
                transaction.process()
                transaction.fail()
            elif status == "settle":
                transaction.settle()
        except TransitionNotAllowed as e:
            # TODO handle this (probably throw something else)
            pass

        transaction.save()

    def execute_transaction(self, transaction):
        """
        :param transaction: A PayU transaction in Initial or Pending state.
        :return: True on success, False on failure.
        """

        if not transaction.payment_processor == self:
            return False

        if transaction.state not in [transaction.States.Initial,
                                     transaction.States.Pending]:
            return False

        return self._charge_transaction(transaction)

    def _charge_transaction(self, transaction):
        token = transaction.payment_method.token

        billing_details = transaction.payment_method.archived_customer
        delivery_details = {
            "DELIVERY_ADDRESS": billing_details["BILL_ADDRESS"],
            "DELIVERY_CITY": billing_details["BILL_CITY"],
            "DELIVERY_EMAIL": billing_details["BILL_EMAIL"],
            "DELIVERY_FNAME": billing_details["BILL_FNAME"],
            "DELIVERY_LNAME": billing_details["BILL_LNAME"],
            "DELIVERY_PHONE": billing_details["BILL_PHONE"]
        }
        payment_details = {
            "AMOUNT": str(transaction.amount),
            "CURRENCY": str(transaction.currency),
            "EXTERNAL_REF": str(transaction.uuid)
        }
        payment_details.update(billing_details)
        payment_details.update(delivery_details)

        payment = TokenPayment(payment_details, token)
        result = payment.pay()

        try:
            if not json.loads(result)["code"]:
                self.update_transaction_status(transaction, "pending")
                return True
            else:
                self.update_transaction_status(transaction, "failed")
                self.transaction.data["result"] = result
        except Exception as e:
            self.update_transaction_status(transaction, "failed")
            self.transaction.data["result"] = str(e)

        return False


@receiver(payment_authorized)
def payu_ipn_received(sender, **kwargs):
    transaction = Transaction.objects.get(uuid=sender.REFNOEXT)
    transaction.payment_processor.update_transaction_status(transaction,
                                                            "settle")


@receiver(alu_token_created)
def payu_token_received(sender, **kwargs):
    transaction = Transaction.objects.get(uuid=sender.ipn.REFNOEXT)

    transaction.payment_method.token = sender.IPN_CC_TOKEN
    transaction.payment_method.verified = True
    transaction.payment_method.save()