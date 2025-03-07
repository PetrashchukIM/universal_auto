import os
import time
import csv
import datetime
import pendulum
import sys
import redis
import re
import html
import json
import time
import logging
import requests
import traceback
from telegram import * 
from telegram.ext import *
from app.models import *
from app.portmone.generate_link import *
from auto.tasks import download_weekly_report_force, send_on_job_application_on_driver_to_Bolt, send_on_job_application_on_driver_to_Uber
from . import bolt, uklon, uber
from scripts.driversrating import DriversRatingMixin
import traceback
import hashlib
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from django.db import IntegrityError

PORT = int(os.environ.get('PORT', '8443'))
DEVELOPER_CHAT_ID = 803129892

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.DEBUG)
logger = logging.getLogger(__name__)

processed_files = []


#Ordering taxi
def start(update, context):
    update.message.reply_text('Привіт! Тебе вітає Універсальне таксі - викликай кнопкою нижче.')
    chat_id = update.message.chat.id
    user = User.get_by_chat_id(chat_id)
    keyboard = [KeyboardButton(text="\U0001f4f2 Надати номер телефону", request_contact=True),
                KeyboardButton(text="\U0001f696 Викликати Таксі", request_location=True),
                KeyboardButton(text="\U0001f4e2 Залишити відгук"),
                KeyboardButton(text="\U0001F4E8 Залишити заявку на роботу")]
    if user:
        user.chat_id = chat_id
        user.save()
        if user.phone_number:
            keyboard = [keyboard[1], keyboard[2], keyboard[3]]
            reply_markup = ReplyKeyboardMarkup(
                keyboard=[keyboard],
                resize_keyboard=True,
                )
            update.message.reply_text('Зробіть вибір', reply_markup=reply_markup)
    else:
        User.objects.create(
            chat_id=chat_id,
            name=update.message.from_user.first_name,
            second_name=update.message.from_user.last_name
        )
        reply_markup = ReplyKeyboardMarkup(
          keyboard=[keyboard],
          resize_keyboard=True,
        )
        update.message.reply_text("Будь ласка розшарьте номер телефону та геолокацію для виклику таксі", reply_markup=reply_markup,)


def update_phone_number(update, context):
    chat_id = update.message.chat.id
    user = User.get_by_chat_id(chat_id)
    phone_number = update.message.contact.phone_number
    if (phone_number and user):
        if len(phone_number) == 12:
            phone_number = f'+{phone_number}'
        user.phone_number = phone_number
        user.chat_id = chat_id
        user.save()
        update.message.reply_text('Дякуємо ми отримали ваш номер телефону для звязку з водієм', reply_markup=ReplyKeyboardRemove())


LOCATION_WRONG = "Місце посадки - невірне"
LOCATION_CORRECT = "Місце посадки - вірне"


def location(update: Update, context: CallbackContext):
    active_drivers = [i.chat_id for i in Driver.objects.all() if i.driver_status == f'{Driver.ACTIVE}']

    if len(active_drivers) == 0:
        report = update.message.reply_text('Вибачте, але зараз немає вільний водіїв. Скористайтеся послугою пізніше', reply_markup=ReplyKeyboardRemove())
        return report
    else:
        if update.edited_message:
            m = update.edited_message
        else:
            m = update.message
        m = context.bot.sendLocation(update.effective_chat.id, latitude=m.location.latitude,
                                     longitude=m.location.longitude, live_period=600)


        context.user_data['latitude'], context.user_data['longitude'] = m.location.latitude, m.location.longitude
        context.user_data['from_address'] = 'Null'
        the_confirmation_of_location(update, context)

        for i in range(1, 10):
            try:
                logger.error(i)
                m = context.bot.editMessageLiveLocation(m.chat_id, m.message_id, latitude=i * 10, longitude=i * 10)
                print(m)
            except Exception as e:
                logger.error(msg=e.message)
                logger.error(i)
            time.sleep(5)


STATE = None # range (1-50)
LOCATION, FROM_ADDRESS, TO_THE_ADDRESS, COMMENT = range(1, 5)
U_NAME, U_SECOND_NAME, U_EMAIL = range(5, 8)


def the_confirmation_of_location(update, context):
    global STATE
    STATE = LOCATION

    keyboard = [KeyboardButton(text=f"\u2705 {LOCATION_CORRECT}"),
                KeyboardButton(text=f"\u274c {LOCATION_WRONG}")]

    reply_markup = ReplyKeyboardMarkup(
        keyboard=[keyboard],
        resize_keyboard=True, )

    update.message.reply_text('Виберіть статус вашої геолокації!', reply_markup=reply_markup)


def from_address(update, context):
    global STATE
    STATE = FROM_ADDRESS
    context.user_data['latitude'], context.user_data['longitude'] = 'Null', 'Null'
    update.message.reply_text('Введіть адресу місця посадки:', reply_markup=ReplyKeyboardRemove())


def to_the_adress(update, context):
    global STATE
    if STATE == FROM_ADDRESS:
        context.user_data['from_address'] = update.message.text
    update.message.reply_text('Введіть адресу місця призначення:', reply_markup=ReplyKeyboardRemove())
    STATE = TO_THE_ADDRESS


def payment_method(update, context):
    global STATE
    STATE = None
    context.user_data['to_the_address'] = update.message.text

    keyboard = [KeyboardButton(text=f"\U0001f4b7 {Order.CASH}"),
                KeyboardButton(text=f"\U0001f4b8 {Order.CARD}")]

    reply_markup = ReplyKeyboardMarkup(
        keyboard=[keyboard],
        resize_keyboard=True,
        )

    update.message.reply_text('Виберіть спосіб оплати:', reply_markup=reply_markup)


WAITING = 'Очікується'


def order_create(update, context):

    keyboard = [
        [
            InlineKeyboardButton("\u2705 Прийняти замовлення", callback_data="Accept order")
        ],

        [
            InlineKeyboardButton("\u274c Відхилити", callback_data="Reject order"),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    payment_method = update.message.text
    context.user_data['payment_method'] = payment_method.split()[1]
    context.user_data['chat_id'] = update.message.chat.id
    user = User.get_by_chat_id(context.user_data['chat_id'])
    context.user_data['phone_number'] = user.phone_number

    drivers = [i.chat_id for i in Driver.objects.all() if i.driver_status == Driver.ACTIVE]

    order = f"Адреса посадки: {context.user_data['from_address']}\nМісце прибуття: {context.user_data['to_the_address']}\n" \
            f"Спосіб оплати: {context.user_data['payment_method']}\nСума: test"

    if len(drivers) != 0:
        for driver in drivers:
            context.bot.send_message(chat_id=driver, text=order, reply_markup=reply_markup)
            #context.bot.send_message(chat_id=736204274, text=order, reply_markup=reply_markup)
    else:
        update.message.reply_text('Вибачте, але вільних водіїв незалишилось')

    create_order = Order.objects.create(
        from_address=context.user_data['from_address'],
        latitude=context.user_data['latitude'],
        longitude=context.user_data['longitude'],
        to_the_address=context.user_data['to_the_address'],
        phone_number=context.user_data['phone_number'],
        chat_id_client=context.user_data['chat_id'],
        sum='',
        payment_method=context.user_data['payment_method'],
        status_order=WAITING)

    create_order.save()


def inline_buttons(update, context):
    query = update.callback_query
    query.answer()

    chat_id = update.effective_chat.id

    if query.data == 'Accept order':
        order = Order.get_order(chat_id_client=context.user_data['chat_id'], sum='', status_order=WAITING)
        if order is not None:
            query.edit_message_text(text=f"Ви обрали: Прийняти замовлення")

            #driver = Driver.get_by_chat_id(chat_id=73620427)
            #order.driver = Driver.objects.get(chat_id=73620427) for develop
            driver = Driver.get_by_chat_id(chat_id=chat_id)
            order.driver = Driver.objects.get(chat_id=chat_id)
            order.status_order = 'Виконується'
            order.save()
            driver.driver_status = Driver.WAIT_FOR_CLIENT
            driver.save()

            vehicle = Vehicle.objects.get(driver=driver.id)

            report_for_client = f'Ваш водій: {driver}\nНазва: {vehicle.name}\nМодель: {vehicle.model}\n' \
                                f'Номер машини: {vehicle}\nПрибуде через: test'

            context.bot.send_message(chat_id=chat_id, text=f'Водій ваш статус зміненно на <<{Driver.WAIT_FOR_CLIENT}>>')
            context.bot.send_message(chat_id=context.user_data['chat_id'], text=report_for_client)
        else:
            query.edit_message_text(text='Замовлення вже виконує інший водій')
    elif query.data == 'Reject order':
        query.edit_message_text(text=f"Ви <<Відмовились від замовлення>>")


# Changing status of driver
def status(update, context):
    chat_id = update.message.chat.id
    driver = Driver.get_by_chat_id(chat_id)
    if driver is not None:
        buttons = [[KeyboardButton(Driver.ACTIVE)],
                    [KeyboardButton(Driver.WITH_CLIENT)],
                    [KeyboardButton(Driver.WAIT_FOR_CLIENT)],
                    [KeyboardButton(Driver.OFFLINE)]
                ]

        context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть статус',
                                 reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True))
    else:
        update.message.reply_text(f'Зареєструйтесь як водій')


def set_status(update, context):
    status = update.message.text
    chat_id = update.message.chat.id
    driver = Driver.get_by_chat_id(chat_id)
    try:
        events = Event.objects.filter(full_name_driver=driver, status_event=False)
        event = [i for i in events]
        event[-1].status_event = True
        event[-1].save()
        update.message.reply_text(f'{driver}: Ваш - {event[-1].event} завершено')
    except:
        pass
    driver.driver_status = status
    driver.save()
    update.message.reply_text(f'Твій статус: <b>{status}</b>', reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)


JOB_DRIVER = 'Водій'


# Add job application
def role_for_job_application(update, context):
    buttons = [[KeyboardButton(f'{JOB_DRIVER}')]]
    context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть посаду на яку ви притендуєте:',
                reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True))


def job_application(update, context):
    role = update.message.text
    chat_id = update.message.chat.id
    user = User.get_by_chat_id(chat_id)
    if (user.phone_number and user.email and user.name and user.second_name):
        JobApplication.objects.create(
            first_name=user.name,
            last_name=user.second_name,
            email=user.email,
            phone_number=user.phone_number,
            role=role)
        update.message.reply_text('Заявку подано', reply_markup=ReplyKeyboardRemove())
        update.message.reply_text('Також вам потрібно зареєструватись на сайті https://supplier.uber.com, як водій')
    else:
        update.message.reply_text('Надайте повну інформацію про себе, щоб залишити заявку. Скористайтесь командою /upd_informations', reply_markup=ReplyKeyboardRemove())


# Update information for users
def update_name(update, context):
    global STATE
    chat_id = update.message.chat.id
    user = User.get_by_chat_id(chat_id)
    if user:
        update.message.reply_text("Введіть Ім`я:")
        STATE = U_NAME
    else:
        update.message.reply_text('Спочатку зареєструйтесь, щоб оновлювати ваші дані')


def update_second_name(update, context):
    global STATE
    name = update.message.text
    name = User.name_and_second_name_validator(name=name)
    if name is not None:
        context.user_data['u_name'] = name
        update.message.reply_text("Введіть Прізвище:")
        STATE = U_SECOND_NAME
    else:
        update.message.reply_text('Ім`я занадто довге. Спробуйте ще раз')


def update_email(update, context):
    global STATE
    second_name = update.message.text
    second_name = User.name_and_second_name_validator(name=second_name)
    if second_name is not None:
        context.user_data['u_second_name'] = second_name
        update.message.reply_text("Введіть електронну адресу:")
        STATE = U_EMAIL
    else:
        update.message.reply_text('Прізвище занадто довге. Спробуйте ще раз')


def update_user_information(update, context):
    global STATE
    email = update.message.text
    chat_id = update.message.chat.id
    user = User.get_by_chat_id(chat_id)
    email = User.email_validator(email=email)
    if email is not None:
        user.name = context.user_data['u_name']
        user.second_name = context.user_data['u_second_name']
        user.email = email
        user.save()
        update.message.reply_text('Ваші дані оновлені')
        STATE = None
        role_for_job_application(update, context)
    else:
        update.message.reply_text('Eлектронна адреса некоректна. Спробуйте ще раз')


# Sending comment
def comment(update, context):
    global STATE
    STATE = COMMENT
    update.message.reply_text('Залишіть відгук або сповістіть о проблемі', reply_markup=ReplyKeyboardRemove())


def save_comment(update, context):
    global STATE
    context.user_data['comment'] = update.message.text
    chat_id = update.message.chat.id

    order = Comment.objects.create(
                comment=context.user_data['comment'],
                chat_id=chat_id)
    order.save()

    STATE = None
    update.message.reply_text('Ваш відгук був збережено. Очікуйте, менеджер скоро з вами звяжеться!')


# Getting id for users
def get_id(update, context):
    chat_id = update.message.chat.id
    update.message.reply_text(f"Ваш id: {chat_id}")


# Create driver and other
USER_DRIVER, USER_MANAGER_DRIVER = 'Водія', 'Менеджера водія'
CREATE_USER, CREATE_VEHICLE = 'Добавити користувача', 'Добавити автомобіль'


# Add users and vehicle to db and others
def add(update, context):
    chat_id = update.message.chat.id
    driver_manager = DriverManager.get_by_chat_id(chat_id)
    keyboard = [[KeyboardButton(f'{CREATE_USER}')],
               [KeyboardButton(f'{CREATE_VEHICLE}')]]

    reply_markup = ReplyKeyboardMarkup(
        keyboard=[keyboard[0], keyboard[1]],
        resize_keyboard=True)
    if driver_manager is not None:
        context.user_data['role'] = driver_manager
        update.message.reply_text('Оберіть опцію, що ви бажаєте створити', reply_markup=reply_markup)
    else:
        update.message.reply_text("Зареєструйтесь, як менеджер водіїв")


def create(update, context):
    keyboard = [[KeyboardButton(text=f"{USER_DRIVER}")],
                [KeyboardButton(text=f"{USER_MANAGER_DRIVER}")]]
    reply_markup = ReplyKeyboardMarkup(
        keyboard=[keyboard[0], keyboard[1]],
        resize_keyboard=True)

    update.message.reply_text('Оберіть користувача, якого ви бажаєте створити', reply_markup=reply_markup)


def name(update, context):
    global STATE_DM
    context.user_data['role'] = update.message.text
    update.message.reply_text("Введіть Ім`я:", reply_markup=ReplyKeyboardRemove())
    STATE_DM = NAME


def second_name(update, context):
    global STATE_DM
    name = update.message.text
    name = User.name_and_second_name_validator(name=name)
    if name is not None:
        context.user_data['name'] = name
        update.message.reply_text("Введіть Прізвище:")
        STATE_DM = SECOND_NAME
    else:
        update.message.reply_text('Ім`я занадто довге. Спробуйте ще раз')


def email(update, context):
    global STATE_DM
    second_name = update.message.text
    second_name = User.name_and_second_name_validator(name=second_name)
    if second_name is not None:
        context.user_data['second_name'] = second_name
        update.message.reply_text("Введіть електронну адресу:")
        STATE_DM = EMAIL
    else:
        update.message.reply_text('Прізвище занадто довге. Спробуйте ще раз')


def phone_number(update, context):
    global STATE_DM
    email = update.message.text
    email = User.email_validator(email=email)
    if email is not None:
        context.user_data['email'] = email
        update.message.reply_text("Введіть телефонний номер:")
        STATE_DM = PHONE_NUMBER
    else:
        update.message.reply_text('Eлектронна адреса некоректна. Спробуйте ще раз')


def create_user(update, context):
    global STATE_DM
    phone_number = update.message.text
    chat_id = update.message.chat.id
    phone_number = User.phone_number_validator(phone_number=phone_number)
    if phone_number is not None:
        if context.user_data['role'] == USER_DRIVER:
            driver = Driver.objects.create(
                name=context.user_data['name'],
                second_name=context.user_data['second_name'],
                email=context.user_data['email'],
                phone_number=phone_number)

            manager = DriverManager.get_by_chat_id(chat_id)
            manager.driver_id.add(driver.id)
            manager.save()
            update.message.reply_text('Водія було добавленно в базу данних')
        elif context.user_data['role'] == USER_MANAGER_DRIVER:
            DriverManager.objects.create(
                name=context.user_data['name'],
                second_name=context.user_data['second_name'],
                email=context.user_data['email'],
                phone_number=phone_number)

            update.message.reply_text('Менеджера водія було добавленно в базу данних')
        STATE_DM = None
    else:
        update.message.reply_text('Телефонний номер некоректний')


SERVICEABLE = 'Придатна'
BROKEN = 'Зламана'

STATE_D = None    # range(50 - 100)
NUMBERPLATE, REPORT = range(50, 52)


# Changing status car
def status_car(update, context):
    chat_id = update.message.chat.id
    driver = Driver.get_by_chat_id(chat_id)
    if driver is not None:
        buttons = [[KeyboardButton(f'{SERVICEABLE}')], [KeyboardButton(f'{BROKEN}')]]
        context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть статус автомобіля',
                                        reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True))
    else:
        update.message.reply_text(f'Зареєструтесь як водій', reply_markup=ReplyKeyboardRemove())


def numberplate(update, context):
    global STATE_D
    context.user_data['status'] = update.message.text
    update.message.reply_text('Введіть номер автомобіля', reply_markup=ReplyKeyboardRemove())
    STATE_D = NUMBERPLATE


def change_status_car(update, context):
    global STATE_D
    context.user_data['licence_place'] = update.message.text.upper()
    number_car = context.user_data['licence_place']
    numberplates = [i.licence_plate for i in Vehicle.objects.all()]
    if number_car in numberplates:
        vehicle = Vehicle.get_by_numberplate(number_car)
        vehicle.car_status = context.user_data['status']
        vehicle.save()
        numberplates.clear()
        update.message.reply_text('Статус авто був змінений')
    else:
        update.message.reply_text('Цього номера немає в базі даних або надіслано неправильні дані. Зверніться до менеджера або повторіть команду')

    STATE_D = None


SEND_REPORT_DEBT = 'Надіслати звіт про оплату заборгованості'


# Sending report for drivers(payment debt)
def sending_report(update, context):
    chat_id = update.message.chat.id
    driver = Driver.get_by_chat_id(chat_id)
    if driver is not None:
        buttons = [[KeyboardButton(f'{SEND_REPORT_DEBT}')]]
        context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть опцію:',
                                 reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True))
    else:
        update.message.reply_text(f'Зареєструтесь як водій', reply_markup=ReplyKeyboardRemove())


def get_debt_photo(update, context):
    update.message.reply_text('Надішліть фото оплати заборгованості', reply_markup=ReplyKeyboardRemove())


def save_debt_report(update, context):
    chat_id = update.message.chat.id
    driver = Driver.get_by_chat_id(chat_id)
    image = update.message.photo[-1].get_file()
    filename = f'{image["file_unique_id"]}.jpg'
    image.download(filename)
    Report_of_driver_debt.objects.create(
                                driver=driver,
                                image=f'static/{filename}')
    update.message.reply_text('Ваш звіт збережено')


# Viewing broken car
def broken_car(update, context):
    chat_id = update.message.chat.id
    driver_manager = DriverManager.get_by_chat_id(chat_id)
    if driver_manager is not None:
        vehicle = Vehicle.objects.filter(car_status=f'{BROKEN}')
        report = ''
        result = [f'{i.licence_plate}' for i in vehicle]
        if len(result) == 0:
            update.message.reply_text("Немає зламаних авто")
        else:
            for i in result:
                report += f'{i}\n'
            update.message.reply_text(f'{report}')
    else:
        update.message.reply_text('Зареєструйтесь як менеджер водіїв')


STATE_DM = None     # range (100 -150)
NAME, SECOND_NAME, EMAIL, PHONE_NUMBER = range(100, 104)
STATUS, DRIVER, CAR_NUMBERPLATE, RATE, NAME_VEHICLE, MODEL_VEHICLE, LICENCE_PLATE_VEHICLE, VIN_CODE_VEHICLE = range(104, 112)
JOB_APPLICATION = range(112, 113)


# Viewing status driver
def driver_status(update, context):
    global STATE_DM
    chat_id = update.message.chat.id
    driver_manager = DriverManager.get_by_chat_id(chat_id)
    if driver_manager is not None:
        buttons = [[KeyboardButton(f'- {Driver.ACTIVE}')],
                   [KeyboardButton(f'- {Driver.WITH_CLIENT}')],
                   [KeyboardButton(f'- {Driver.WAIT_FOR_CLIENT}')],
                   [KeyboardButton(f'- {Driver.OFFLINE}')]
                   ]
        STATE_DM = STATUS
        context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть статус',
                                 reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True))
    else:
        update.message.reply_text('Зареєструйтесь як менеджер водіїв')


def viewing_status_driver(update, context):
    global STATE_DM
    status = update.message.text
    status = status[2:]
    driver = Driver.objects.filter(driver_status=status)
    report = ''
    result = [f'{i.name} {i.second_name}' for i in driver]
    if len(result) == 0:
        update.message.reply_text('Зараз немає водіїв з таким статусом', reply_markup=ReplyKeyboardRemove())
    else:
        for i in result:
            report += f'{i}\n'
    update.message.reply_text(f'{report}', reply_markup=ReplyKeyboardRemove())
    STATE_DM = None


TAKE_A_DAY_OFF = 'Взяти вихідний'
TAKE_SICK_LEAVE = 'Взяти лікарняний'
SIGN_UP_FOR_A_SERVICE_CENTER = 'Записатись до сервісного центру'
REPORT_CAR_DAMAGE = 'Оповістити про пошкодження авто'


def option(update, context):
    chat_id = update.message.chat.id
    driver = Driver.get_by_chat_id(chat_id)
    keyboard = [KeyboardButton(text=f"{SIGN_UP_FOR_A_SERVICE_CENTER}"),
                KeyboardButton(text=f"{REPORT_CAR_DAMAGE}"),
                KeyboardButton(text=f"{TAKE_A_DAY_OFF}"),
                KeyboardButton(text=f"{TAKE_SICK_LEAVE}")]
    if driver is not None:
        reply_markup = ReplyKeyboardMarkup(
            keyboard=[keyboard],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        update.message.reply_text('Оберіть опцію: ', reply_markup=reply_markup)
    else:
        update.message.reply_text(f'Зареєструтесь як водій', reply_markup=ReplyKeyboardRemove())


def take_a_day_off_or_sick_leave(update, context):
    event = update.message.text
    chat_id = update.message.chat.id
    event = event.split()
    driver = Driver.get_by_chat_id(chat_id)
    events = Event.objects.filter(full_name_driver=driver, status_event=False)
    list_event = [i for i in events]
    if len(list_event) > 0:
        update.message.reply_text(f"У вас вже відкритий <<Лікарняний>> або <<Вихідний>>.\nЩоб закрити подію скористайтесь командою /status")
    else:
        driver.driver_status = f'{Driver.OFFLINE}'
        driver.save()
        Event.objects.create(
                full_name_driver=driver,
                event=event[1].title(),
                chat_id=chat_id,
                created_at=datetime.datetime.now())
        update.message.reply_text(f'Ваш статус зміненно на <<{Driver.OFFLINE}>> та ваш <<{event[1].title()}>> розпочато',
                                            reply_markup=ReplyKeyboardRemove())


# Add Vehicle to driver
def get_list_drivers(update, context):
    global STATE_DM
    chat_id = update.message.chat.id
    driver_manager = DriverManager.get_by_chat_id(chat_id)
    if driver_manager is not None:
        drivers = {i.id: f'{i.name } {i.second_name}' for i in Driver.objects.all()}
        if len(drivers) == 0:
            update.message.reply_text('Кількість зареєстрованих водіїв 0')
        else:
            drivers_keys = sorted(drivers)
            drivers = {i: drivers[i] for i in drivers_keys}
            report_list_drivers = ''
            for k, v in drivers.items():
                report_list_drivers += f'{k}: {v}\n'
            update.message.reply_text(f'{report_list_drivers}')
            STATE_DM = DRIVER
            update.message.reply_text('Укажіть номер водія, якому хочете добавити авто.')
    else:
        update.message.reply_text('Зареєструйтесь як менеджер водіїв')


def get_list_vehicle(update, context):
    global STATE_DM
    id_driver = update.message.text
    try:
        id_driver = int(id_driver)
        context.user_data['driver'] = Driver.objects.get(id=id_driver)
    except:
        update.message.reply_text('Не вдалось обробити ваше значення, або переданий номер водія виявився недійсним. Спробуйте ще раз')
    vehicles = {i.id: i.licence_plate for i in Vehicle.objects.all()}
    if len(vehicles) == 0:
        update.message.reply_text('Кількисть зареєстрованих траспортних засобів 0')
    else:
        if context.user_data['driver'] is not None:
            report_list_vehicles = ''
            for k, v in vehicles.items():
                report_list_vehicles += f'{k}: {v}\n'
            update.message.reply_text(f'{report_list_vehicles}')
            STATE_DM = CAR_NUMBERPLATE
            update.message.reply_text('Укажіть номер авто, який ви хочете прикріпити до водія')


F_UKLON, F_UBER, F_BOLT = 'NewUklon', 'Uber', 'Bolt'


def get_fleet(update, context):
    id_vehicle = update.message.text
    try:
        id_vehicle = int(id_vehicle)
        context.user_data['vehicle'] = Vehicle.objects.get(id=id_vehicle)
    except:
        update.message.reply_text('Не вдалось обробити ваше значення, або переданий номер автомобільного номера виявився недійсним. Спробуйте ще раз')
    if context.user_data['vehicle'] is not None:
        buttons = [[KeyboardButton(F_UKLON)],
                   [KeyboardButton(F_UBER)],
                   [KeyboardButton(F_BOLT)]]
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='Оберіть автопарк. Для прикріплення автомобіля водію',
                                 reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True))


def get_driver_external_id(update, context):
    global STATE_DM
    fleet = update.message.text
    context.user_data['fleet'] = fleet
    try:
        response = Fleets_drivers_vehicles_rate.objects.get(
            fleet=Fleet.objects.get(name=fleet),
            driver=context.user_data['driver'],
            vehicle=context.user_data['vehicle'])
        response = str(response)
    except:
        if fleet == F_UKLON:
            try:
                driver = str(context.user_data['driver'])
                driver = driver.split()
                driver = f'{driver[1]} {driver[0]}'
                driver_external_id = NewUklonPaymentsOrder.objects.get(full_name=driver)
                driver_external_id = driver_external_id.signal
            except:
                pass
        elif fleet == F_BOLT:
            try:
                driver_external_id = BoltPaymentsOrder.objects.get(driver_full_name=str(context.user_data['driver']))
                driver_external_id = driver_external_id.mobile_number
            except:
                pass
        else:
            try:
                driver = str(context.user_data['driver'])
                driver = driver.split()
                driver_external_id = UberPaymentsOrder.objects.get(first_name=driver[0], last_name=driver[1])
                driver_external_id = driver_external_id.driver_uuid
            except:
                pass

        try:
            context.user_data['driver_external_id'] = driver_external_id
        except:
            context.user_data['driver_external_id'] = 'pass'

        drivers_rate = {key: round(key * 0.05, 2) for key in range(1, 21)}
        rate = ''
        for k, v in drivers_rate.items():
            rate += f'{k}: {v}\n'

        context.user_data['rate'] = drivers_rate
        update.message.reply_text(f"{rate}",  reply_markup=ReplyKeyboardRemove())
        update.message.reply_text(f"Укажіть номер рейтингу, який ви хочете встановити для {context.user_data['driver']} в автопарку {context.user_data['fleet']}")
        STATE_DM = RATE
    try:
        if isinstance(response, str):
            update.message.reply_text('Для даного водія вже прикріплене данне авто та автопарк. Спробуйте спочатку')
            STATE_DM = None
    except:
        pass


def add_information_to_driver(update, context):
    global STATE_DM
    id_rate = update.message.text
    try:
        id_rate = int(id_rate)
        rate = context.user_data['rate']
        rate = rate[id_rate]
    except:
        update.message.reply_text('Не вдалось обробити ваше значення, або переданий номер рейтингу не є дійсним. Спробуйте ще раз')
    if isinstance(rate, float):
        Fleets_drivers_vehicles_rate.objects.create(
                fleet=Fleet.objects.get(name=context.user_data['fleet']),
                driver=context.user_data['driver'],
                vehicle=context.user_data['vehicle'],
                driver_external_id=context.user_data['driver_external_id'],
                rate=rate)
        update.message.reply_text(f"Ви добавили водію машину та рейтинг в автопарк {context.user_data['fleet']}")
        if context.user_data['driver_external_id'] == 'pass':
            update.message.reply_text(f"Водія {context.user_data['driver']} збереженно зі значенням driver_external_id = \
                        {context.user_data['driver_external_id']}. Ви можете його змінити власноруч, через панель адміністратора")
        STATE_DM = None


# Push job application to fleets
def get_list_job_application(update, context):
    global STATE_DM
    chat_id = update.message.chat.id
    driver_manager = DriverManager.get_by_chat_id(chat_id)
    if driver_manager is not None:
        applications = {i.id: f'{i}' for i in JobApplication.objects.all() if (i.role == f'{JOB_DRIVER}' and i.status_job_application == False)}
        if len(applications) == 0:
            update.message.reply_text('Заявок на роботу водія поки немає')
        else:
            report_list_applications = ''
            for k, v in applications.items():
                report_list_applications += f'{k}: {v}\n'
            update.message.reply_text(report_list_applications)
            update.message.reply_text('Укажіть номер користувача, заявку якого ви бажаєте відправити')
            STATE_DM = JOB_APPLICATION
    else:
        update.message.reply_text('Зареєструйтесь як менеджер водіїв')


def get_fleet_for_job_application(update, context):
    global STATE_DM
    id_job_application = update.message.text
    try:
        id_job_application = int(id_job_application)
        context.user_data['job_application'] = JobApplication.objects.get(id=id_job_application)
        buttons = [[KeyboardButton(f'- {F_BOLT}')],
                   [KeyboardButton(f'- {F_UBER}')]]
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='Оберіть автопарк. Куди ви бажаєте подати заявку',
                                 reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True))
        STATE_DM = None
    except:
        update.message.reply_text('Не вдалось обробити ваше значення. Спробуйте ще раз')


def add_job_application_to_fleet(update, context):
    response = update.message.text
    data = context.user_data['job_application']
    if response == f'- {F_BOLT}':
        send_on_job_application_on_driver_to_Bolt.delay(email=data.email, phone_number=data.phone_number)
        update.message.reply_text('Заявка була додана в автопарк Bolt', reply_markup=ReplyKeyboardRemove())
    elif response == f'- {F_UBER}':
        send_on_job_application_on_driver_to_Uber.delay(phone_number=data.phone_number,
                                                        email=data.email,
                                                        name=data.first_name,
                                                        second_name=last_name)

        update.message.reply_text('Заявка була додана в автопарк Uber', reply_markup=ReplyKeyboardRemove())
        update.message.reply_text('Якщо заявки немає в автопарку, користувачу потрібно зареєструватись на сайті як водій')


# Add vehicle to db
def name_vehicle(update, context):
    global STATE_DM
    update.message.reply_text('Введіть назву авто:', reply_markup=ReplyKeyboardRemove())
    STATE_DM = NAME_VEHICLE


def get_name_vehicle(update, context):
    global STATE_DM
    name_vehicle = update.message.text
    name_vehicle = Vehicle.name_validator(name=name_vehicle)
    if name_vehicle is not None:
        context.user_data['name_vehicle'] = name_vehicle
        update.message.reply_text('Введіть модель авто:')
        STATE_DM = MODEL_VEHICLE
    else:
        update.message.reply_text('Назва занадто довга. Спробуйте ще раз')


def get_model_vehicle(update, context):
    global STATE_DM
    model_vehicle = update.message.text
    model_vehicle = Vehicle.model_validator(model=model_vehicle)
    if model_vehicle is not None:
        context.user_data['model_vehicle'] = model_vehicle
        update.message.reply_text('Введіть автомобільний номер:')
        STATE_DM = LICENCE_PLATE_VEHICLE
    else:
        update.message.reply_text('Назва занадто довга. Спробуйте ще раз')


def get_licence_plate_vehicle(update, context):
    global STATE_DM
    licence_plate_vehicle = update.message.text
    licence_plate_vehicle = Vehicle.licence_plate_validator(licence_plate=licence_plate_vehicle)
    if licence_plate_vehicle is not None:
        context.user_data['licence_plate_vehicle'] = licence_plate_vehicle
        update.message.reply_text('Введіть vin_code для машини (максимальна кількість символів 17)')
        STATE_DM = VIN_CODE_VEHICLE
    else:
        update.message.reply_text('Номерний знак занадто довгий. Спробуйте ще раз')


def get_vin_code_vehicle(update, context):
    global STATE_DM
    vin_code = update.message.text
    vin_code = Vehicle.vin_code_validator(vin_code=vin_code)
    if vin_code is not None:
        Vehicle.objects.create(
            name=context.user_data['name_vehicle'],
            model=context.user_data['model_vehicle'],
            licence_plate=context.user_data['licence_plate_vehicle'],
            vin_code=vin_code)
        update.message.reply_text('Машину додано до бази даних')
        STATE_DM = None
    else:
        update.message.reply_text('Vin code занадто довгий. Спробуйте ще раз')


STATE_SSM = None   # range(150-200)
LICENCE_PLATE, PHOTO, START_OF_REPAIR, END_OF_REPAIR = range(150, 154)


# Sending report on repair
def numberplate_car(update, context):
    global STATE_SSM
    chat_id = update.message.chat.id
    service_station_manager = ServiceStationManager.get_by_chat_id(chat_id)
    if service_station_manager is not None:
        STATE_SSM = LICENCE_PLATE
        update.message.reply_text('Будь ласка, введіть номерний знак автомобіля')
    else:
        update.message.reply_text('Зареєструйтесь як менеджер сервісного центру')


def photo(update, context):
    global STATE_SSM
    context.user_data['licence_plate'] = update.message.text.upper()
    numberplates = [i.licence_plate for i in Vehicle.objects.all()]
    if context.user_data['licence_plate'] not in numberplates:
        update.message.reply_text('Написаного вами номера немає в базі, зверніться до менеджера парку')
    STATE_SSM = PHOTO
    update.message.reply_text('Будь ласка, надішліть мені фото звіту про ремонт (Одне фото)')


def start_of_repair(update, context):
    global STATE_SSM
    context.user_data['photo'] = update.message.photo[-1].get_file()
    update.message.reply_text('Будь ласка, введіть дату та час початку ремонту у форматі: %Y-%m-%d %H:%M:%S')
    STATE_SSM = START_OF_REPAIR


def end_of_repair(update, context):
    global STATE_SSM
    context.user_data['start_of_repair'] = update.message.text + "+00"
    try:
        time.strptime(context.user_data['start_of_repair'], "%Y-%m-%d %H:%M:%S+00")
    except ValueError:
        update.message.reply_text('Недійсна дата')
    STATE_SSM = END_OF_REPAIR
    update.message.reply_text("Будь ласка, введіть дату та час закінченяя ремонту у форматі: %Y-%m-%d %H:%M:%S")


def send_report_to_db_and_driver(update, context):
    global STATE_SSM
    context.user_data['end_of_repair'] = update.message.text + '+00'
    try:
        time.strptime(context.user_data['end_of_repair'], "%Y-%m-%d %H:%M:%S+00")
    except ValueError:
        update.message.reply_text('Недійсна дата')

    order = RepairReport(
                    repair=context.user_data['photo']["file_path"],
                    numberplate=context.user_data['licence_plate'],
                    start_of_repair=context.user_data['start_of_repair'],
                    end_of_repair=context.user_data['end_of_repair'])
    order.save()
    STATE_SSM = None
    update.message.reply_text('Ваш звіт збережено в базі даних')


def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'An exception was raised while handling an update\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # Finally, send the message
    context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=message, parse_mode=ParseMode.HTML)


def code(update: Update, context: CallbackContext):
    pattern = r'^\d{4}$'
    m = update.message.text
    if re.match(pattern, m) is not None:
        r = redis.Redis.from_url(os.environ["REDIS_URL"])
        r.publish('code', update.message.text)
        update.message.reply_text('Формування звіту...')
        context.bot.send_chat_action(chat_id=update.effective_message.chat_id, action=ChatAction.TYPING)
    else:
        update.message.reply_text('Боту не вдалось опрацювати ваше повідомлення. Спробуйте пізніше')


def help(update, context) -> str:
    update.message.reply_text('Для першого кроку зробіть реєстрацію або авторизуйтеся командою /start \n' \
                              'Щоб переглянути команди для вашої ролі скористайтесь командою /get_information \n')


STATE_O = None     # range(200-250)
CARD, SUM, PORTMONE_SUM, PORTMONE_COMMISSION, GENERATE_LINK = range(200, 205)

TRANSFER_MONEY = 'Перевести кошти'
GENERATE_LINK = 'Сгенерувати лінк'


# Transfer money
def payments(update, context):
    chat_id = update.message.chat.id
    owner = Owner.get_by_chat_id(chat_id)
    if owner is not None:
        buttons = [[KeyboardButton(f'{TRANSFER_MONEY}')],
                   [KeyboardButton(f'{GENERATE_LINK}')]]
        context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть опцію:',
                                reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True))
    else:
        update.message.reply_text('Ця команда тільки для власника')


def get_card(update, context):
    global STATE_O
    update.message.reply_text('Введіть номер картки отримувача', reply_markup=ReplyKeyboardRemove())
    STATE_O = CARD


def get_sum(update, context):
    global STATE_O
    card = update.message.text
    card = Privat24.card_validator(card=card)
    if card is not None:
        context.user_data['card'] = card
        update.message.reply_text('Введіть суму в форматі DD.CC')
        STATE_O = SUM
    else:
        update.messega.reply_text('Введена карта невалідна')


THE_DATA_IS_CORRECT = "Транзакція заповнена вірно"
THE_DATA_IS_WRONG = "Транзакція заповнена невірно"


def transfer(update, context):
    global STATE_O
    global p

    buttons = [[KeyboardButton(f'{THE_DATA_IS_CORRECT}')],
               [KeyboardButton(f'{THE_DATA_IS_WRONG}')]]

    context.user_data['sum'] = update.message.text

    p = Privat24(card=context.user_data['card'], sum=context.user_data['sum'], driver=True, sleep=7, headless=True)
    p.login()
    p.password()
    p.money_transfer()

    context.bot.send_photo(chat_id=update.effective_chat.id, photo=open('privat_3.png', 'rb'))
    context.bot.send_message(chat_id=update.effective_chat.id, text='Оберіть опцію:',
                             reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True))
    STATE_O = None


def correct_transfer(update, context):
    p.transfer_confirmation()
    update.message.reply_text("Транзакція пройшла успішно")


def wrong_transfer(update, context):
    update.message.reply_text("Транзакція відмінена")
    p.quit()


# Generate link debt
COMMISSION_ONLY_PORTMONE = 'Використати стандартну комісію'
MY_COMMISSION = "Встановити свою комісію"


def commission(update, context):
    buttons = [[KeyboardButton(f'{COMMISSION_ONLY_PORTMONE}')],
               [KeyboardButton(f'{MY_COMMISSION}')]]
    context.bot.send_message(chat_id=update.effective_chat.id, text='Виберіть, яку комісію бажаєте встановити:',
                             reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True))


def get_my_commission(update, context):
    global STATE_O
    update.message.reply_text(
        "Введіть суму комісії в форматі DD.CC (ваша комісія з комісієй сервісу Portmone буде вирахувана від загальної суми)")
    STATE_O = PORTMONE_COMMISSION


def get_sum_for_portmone(update, context):
    global STATE_O
    if STATE_O == PORTMONE_COMMISSION:
        commission = update.message.text
        commission = conversion_to_float(sum=commission)
        if commission is not None:
            context.user_data['commission'] = commission
            update.message.reply_text(f'Введіть суму на яку ви хочете виставити запит, в форматі DD.CC')
            STATE_O = GENERATE_LINK
            STATE_O = GENERATE_LINK
        else:
            update.message.reply_text('Не вдалось опрацювати суму вашої комісії, спробуйте ще раз')
    else:
        update.message.reply_text(f'Введіть суму на яку ви хочете виставити запит, в форматі DD.CC')
        STATE_O = PORTMONE_SUM


def generate_link_v1(update, context):
    global STATE_O
    sum = update.message.text
    n_sum = conversion_to_float(sum=sum)
    if n_sum is not None:
        p = Portmone(sum=n_sum)
        result = p.get_link()
        update.message.reply_text(f'{result}')
        STATE_O = None
    else:
        update.message.reply_text('Не вдалось обробити вашу суму, спробуйте ще раз')


def generate_link_v2(update, context):
    global STATE_O
    sum = update.message.text
    n_sum = conversion_to_float(sum=sum)
    if n_sum is not None:
        p = Portmone(sum=n_sum, commission=context.user_data['commission'])
        result = p.get_link()
        update.message.reply_text(f'{result}')
        STATE_O = None
    else:
        update.message.reply_text('Не вдалось обробити вашу суму, спробуйте ще раз')


def get_information(update, context):
    chat_id = update.message.chat.id
    driver_manager = DriverManager.get_by_chat_id(chat_id)
    driver = Driver.get_by_chat_id(chat_id)
    manager = ServiceStationManager.get_by_chat_id(chat_id)
    owner = Owner.get_by_chat_id(chat_id)
    standart_commands = '/start - Щоб зареєструватись та замовити таксі\n' \
                        '/help - Допомога\n' \
                        '/id - Дізнатись id\n'
    if driver is not None:
        report = 'Стандарті команди: \n\n' \
                f'{standart_commands}\n' \
                'Для вашої ролі:\n\n' \
                '/status - Змінити статус водія\n' \
                '/status_car - Змінити статус автомобіля\n' \
                '/sending_report - Відправити звіт про оплату заборгованості\n'
        update.message.reply_text(f'{report}')
    elif driver_manager is not None:
        report = 'Стандарті команди: \n\n' \
                f'{standart_commands}\n' \
                'Для вашої ролі:\n\n' \
                '/car_status - Показати всі зломлені машини\n' \
                '/driver_status - Показати водіїв за їх статусом\n' \
                '/add - Створити користувачів та автомобілі\n' \
                '/add_vehicle_to_driver - Добавити водію автомобіль\n' \
                '/add_job_application_to_fleets - Добавити водія в автопарк\n' \
                '/option - Взяти вихідний/лікарняний/Сповістити про пошкодження/Записатист до СТО\n'
        update.message.reply_text(f'{report}')
    elif manager is not None:
        report = 'Стандарті команди: \n\n' \
                f'{standart_commands}\n' \
                'Для вашої ролі:\n\n' \
                '/send_report - Відправити звіт про ремонт\n'
        update.message.reply_text(f'{report}')
    elif owner is not None:
        report = 'Стандарті команди: \n\n' \
                f'{standart_commands}\n' \
                'Для вашої ролі:\n\n' \
                '/report - Загрузити та побачити недільні звіти\n' \
                '/rating - Побачити рейтинг водіїв\n' \
                '/payment - Перевести кошти або сгенерити лінк на оплату\n' \
                '/download_report - Загрузити тижневі звіти\n'
        update.message.reply_text(f'{report}')
    else:
        update.message.reply_text(f'{standart_commands}')


def text(update, context):
    """ STATE - for all users, STATE_D - for drivers, STATE_O - for owner,
            STATE_DM - for driver manager, STATE_SSM - for service station manager"""
    global STATE
    global STATE_O
    global STATE_D
    global STATE_DM
    global STATE_SSM

    if STATE is not None:
        if STATE == FROM_ADDRESS:
            return to_the_adress(update, context)
        elif STATE == TO_THE_ADDRESS:
            return payment_method(update, context)
        elif STATE == COMMENT:
            return save_comment(update, context)
        elif STATE == U_NAME:
            return update_second_name(update, context)
        elif STATE == U_SECOND_NAME:
            return update_email(update, context)
        elif STATE == U_EMAIL:
            return update_user_information(update, context)
    elif STATE_D is not None:
        if STATE_D == NUMBERPLATE:
            return change_status_car(update, context)
    elif STATE_O is not None:
        if STATE_O == CARD:
            return get_sum(update, context)
        elif STATE_O == SUM:
            return transfer(update, context)
        elif STATE_O == PORTMONE_SUM:
            return generate_link_v1(update, context)
        elif STATE_O == PORTMONE_COMMISSION:
            return get_sum_for_portmone(update, context)
        elif STATE_O == GENERATE_LINK:
            return generate_link_v2(update, context)
    elif STATE_DM is not None:
        if STATE_DM == STATUS:
            return viewing_status_driver(update, context)
        elif STATE_DM == NAME:
            return second_name(update, context)
        elif STATE_DM == SECOND_NAME:
            return email(update, context)
        elif STATE_DM == EMAIL:
            return phone_number(update, context)
        elif STATE_DM == PHONE_NUMBER:
            return create_user(update, context)
        elif STATE_DM == DRIVER:
            return get_list_vehicle(update, context)
        elif STATE_DM == CAR_NUMBERPLATE:
            return get_fleet(update, context)
        elif STATE_DM == RATE:
            return add_information_to_driver(update, context)
        elif STATE_DM == NAME_VEHICLE:
            return get_name_vehicle(update, context)
        elif STATE_DM == MODEL_VEHICLE:
            return get_model_vehicle(update, context)
        elif STATE_DM == LICENCE_PLATE_VEHICLE:
            return get_licence_plate_vehicle(update, context)
        elif STATE_DM == VIN_CODE_VEHICLE:
            return get_vin_code_vehicle(update, context)
        elif STATE_DM == JOB_APPLICATION:
            return get_fleet_for_job_application(update, context)
    elif STATE_SSM is not None:
        if STATE_SSM == LICENCE_PLATE:
            return photo(update, context)
        elif STATE_SSM == PHOTO:
            return start_of_repair(update, context)
        elif STATE_SSM == START_OF_REPAIR:
            return end_of_repair(update, context)
        elif STATE_SSM == END_OF_REPAIR:
            return send_report_to_db_and_driver(update, context)
    else:
        return code(update, context)


def drivers_rating(update, context):
    text = 'Рейтинг водіїв\n\n'
    # for fleet in DriversRatingMixin().get_rating():
    #     text += fleet['fleet'] + '\n'
    #     for period in fleet['rating']:
    #         text += f"{period['start']:%d.%m.%Y} - {period['end']:%d.%m.%Y}" + '\n'
    #         if period['rating']:
    #             text += '\n'.join([f"{item['num']} {item['driver']} {item['amount']:15.2f} - {item['trips'] if item['trips']>0 else ''}" for item in period['rating']]) + '\n\n'
    #         else:
    #             text += 'Отримання даних... Спробуйте пізніше\n'
    update.message.reply_text(text)


def report(update, context):
    update.message.reply_text('Ваш запит прийнято.\nМи надішлемо вам звіт, як тільки він сформується')
    update.message.reply_text("Введіть ваш Uber OTP код з SMS, якщо ви отримали його")
    
    report = get_report()
    owner, totals = report[0], report[1]
    drivers = {f'{i.name} {i.second_name}': i.chat_id for i in Driver.objects.all()}

    # sending report to owner
    message = f'Fleet Owner: {"%.2f" % owner["Fleet Owner"]}\n\n' + '\n'.join(totals.values())
    context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=message)

    # sending report to driver
    if len(drivers) != 0:
        for driver in drivers:
            try:
                message, chat_id = totals[f'{driver}'], drivers[f'{driver}']
                context.bot.send_message(chat_id=chat_id, text=message)
            except:
                pass


def auto_report_for_driver_and_owner(context):
    report = get_report()
    owner, totals = report[0], report[1]
    drivers = {f'{i.name} {i.second_name}': i.chat_id for i in Driver.objects.all()}

    # sending report to owner
    message = f'Fleet Owner: {"%.2f" % owner["Fleet Owner"]}\n\n' + '\n'.join(totals.values())
    context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=message)

    # sending report to driver
    if len(drivers) != 0:
        for driver in drivers:
            try:
                message, chat_id = totals[f'{driver}'], drivers[f'{driver}']
                context.bot.send_message(chat_id=chat_id, text=message)
            except:
                pass


def download_report(update, context):
    update.message.reply_text("Запит на завантаження щотижневого звіту подано")
    download_weekly_report_force.delay()


def cancel(update, context):
    global STATE
    global STATE_D
    global STATE_O
    global STATE_DM
    global STATE_SSM

    STATE, STATE_D, STATE_O, STATE_DM, STATE_SSM = None, None, None, None, None


#Need fix
def update_db(update, context):
    """Pushing data to database from weekly_csv files"""
    # getting and opening files
    directory = '../app'
    files = os.listdir(directory)

    UberPaymentsOrder.download_weekly_report()
    UklonPaymentsOrder.download_weekly_report()
    BoltPaymentsOrder.download_weekly_report()

    files = os.listdir(directory)
    files_csv = filter(lambda x: x.endswith('.csv'), files)
    list_new_files = list(set(files_csv)-set(processed_files))

    if len(list_new_files) == 0:
        update.message.reply_text('No new updates yet')
    else:
        update.message.reply_text('Please wait')
        for name_file in list_new_files:
            processed_files.append(name_file)
            with open(f'{directory}/{name_file}', encoding='utf8') as file:
                if 'Куцко - Income_' in name_file:
                    UklonPaymentsOrder.parse_and_save_weekly_report_to_database(file=file)
                elif '-payments_driver-___.csv' in name_file:
                    UberPaymentsOrder.parse_and_save_weekly_report_to_database(file=file)
                elif 'Kyiv Fleet 03_232 park Universal-auto.csv' in name_file:
                    BoltPaymentsOrder.parse_and_save_weekly_report_to_database(file=file)

        FileNameProcessed.save_filename_to_db(processed_files)
        list_new_files.clear()
        update.message.reply_text('Database updated')


def save_reports(update, context):
    wrf = WeeklyReportFile()
    wrf.save_weekly_reports_to_db()
    update.message.reply_text("Reports have been saved")


def get_owner_today_report(update, context) -> str:
    pass


def get_driver_today_report(update, context) -> str:
    driver_first_name = User.objects.filter(user_id = {update.message.chat.id})
    driver_ident = PaymentsOrder.objects.filter(driver_uuid='')
    if user.type == 0:
        data = PaymentsOrder.objects.filter(transaction_time = date.today(), driver_uuid = {driver_ident} )
        update.message.reply_text(f'Hi {update.message.chat.username} driver')
        update.message.reply_text(text = data)


def get_driver_week_report(update, context) -> str:
    pass


def choice_driver_option(update, context) -> list:
        update.message.reply_text(f'Hi {update.message.chat.username} driver')
        buttons = [[KeyboardButton('Get today statistic')], [KeyboardButton('Choice week number')],[KeyboardButton('Update report')]]
        context.bot.send_message(chat_id=update.effective_chat.id, text='choice option',
        reply_markup=ReplyKeyboardMarkup(buttons))


def get_manager_today_report(update, context) -> str:
    if user.type == 1:
        data = PaymentsOrder.objects.filter(transaction_time = date.today())
        update.message.reply_text(text=data)
    else:
        error_handler()


def get_stat_for_manager(update, context) -> list:
        update.message.reply_text(f'Hi {update.message.chat.username} manager')
        buttons = [[KeyboardButton('Get all today statistic')]]
        context.bot.send_message(chat_id=update.effective_chat.id, text='choice option',
        reply_markup=ReplyKeyboardMarkup(buttons))


def aut_handler(update, context) -> list:
    if 'Get autorizate' in update.message.text:
        if user.type == 0:
            choice_driver_option(update, context)
        elif user.type == 2:
            get_owner_today_report(update, context)
        elif user.type == 1:
            get_stat_for_manager(update, context)
        else:
            update_phone_number()


def get_update_report(update, context):
    user = User.get_by_chat_id(chat_id)
    if user in uklon_drivers_list:
        uklon.run()
        aut_handler(update, context)
    elif username in bolt_drivers_list:
        bolt.run()
        aut_handler(update, context)
    elif username in uber_drivers_list:
        update.message.reply_text("Enter you Uber OTP code from SMS:")
        uber.run()
        aut_handler(update, context)


WEBHOOK_URL = os.environ['WEBHOOK_URL']
bot = Bot(token=os.environ['TELEGRAM_TOKEN'])
updater = Updater(os.environ['TELEGRAM_TOKEN'], use_context=True)
dp = updater.dispatcher


@csrf_exempt
def webhook(request):
    # Command for Owner
    dp.add_handler(CommandHandler("report", report, run_async=True))
    dp.add_handler(CommandHandler("download_report", download_report))
    dp.add_handler(CommandHandler("rating", drivers_rating))

    # Transfer money
    dp.add_handler(CommandHandler("payment", payments))
    dp.add_handler(MessageHandler(Filters.text(f"{TRANSFER_MONEY}"), get_card))
    dp.add_handler(MessageHandler(Filters.text(f"{THE_DATA_IS_CORRECT}"),
                                  correct_transfer))
    dp.add_handler(
        MessageHandler(Filters.text(f"{THE_DATA_IS_WRONG}"), wrong_transfer))

    # Generate link debt
    dp.add_handler(MessageHandler(Filters.text(f"{GENERATE_LINK}"), commission))
    dp.add_handler(MessageHandler(Filters.text(f"{COMMISSION_ONLY_PORTMONE}"),
                                  get_sum_for_portmone))
    dp.add_handler(
        MessageHandler(Filters.text(f"{MY_COMMISSION}"), get_my_commission))

    # Publicly available commands
    # Getting id
    dp.add_handler(CommandHandler("id", get_id))
    # Information on commands
    dp.add_handler(CommandHandler("help", help))
    dp.add_handler(CommandHandler("get_information", get_information))

    # Commands for Users
    # Ordering taxi
    dp.add_handler(CommandHandler("start", start))
    # incomplete auth
    dp.add_handler(MessageHandler(Filters.contact, update_phone_number))
    # ordering taxi
    dp.add_handler(MessageHandler(Filters.location, location, run_async=True))
    dp.add_handler(MessageHandler(Filters.text(f"\u2705 {LOCATION_CORRECT}"),
                                  to_the_adress))
    dp.add_handler(
        MessageHandler(Filters.text(f"\u274c {LOCATION_WRONG}"), from_address))
    dp.add_handler(MessageHandler(
        Filters.text(f"\U0001f4b7 {Order.CASH}") |
        Filters.text(f"\U0001f4b8 {Order.CARD}"),
        order_create))
    # sending comment
    dp.add_handler(
        MessageHandler(Filters.text("\U0001f4e2 Залишити відгук"), comment))
    # Add job application
    dp.add_handler(
        MessageHandler(Filters.text("\U0001F4E8 Залишити заявку на роботу"),
                       role_for_job_application))
    dp.add_handler(
        MessageHandler(Filters.text(f'{JOB_DRIVER}'), job_application))

    # Update information for users
    dp.add_handler(CommandHandler("upd_informations", update_name))

    # Commands for Drivers
    # Changing status of driver
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(MessageHandler(
        Filters.text(Driver.ACTIVE) |
        Filters.text(Driver.WITH_CLIENT) |
        Filters.text(Driver.WAIT_FOR_CLIENT) |
        Filters.text(Driver.OFFLINE),
        set_status))

    # Updating status_car
    dp.add_handler(CommandHandler("status_car", status_car))
    dp.add_handler(MessageHandler(
        Filters.text(f'{SERVICEABLE}') |
        Filters.text(f'{BROKEN}'),
        numberplate))

    # Sending report(payment debt)
    dp.add_handler(CommandHandler("sending_report", sending_report))
    dp.add_handler(
        MessageHandler(Filters.text(f'{SEND_REPORT_DEBT}'), get_debt_photo))
    dp.add_handler(MessageHandler(Filters.photo, save_debt_report))

    # Take a day off/Take sick leave
    dp.add_handler(CommandHandler("option", option))
    dp.add_handler(MessageHandler(
        Filters.text(f'{TAKE_A_DAY_OFF}') |
        Filters.text(f'{TAKE_SICK_LEAVE}'),
        take_a_day_off_or_sick_leave))

    # Commands for Driver Managers
    # Returns status cars
    dp.add_handler(CommandHandler("car_status", broken_car))
    # Viewing status driver
    dp.add_handler(CommandHandler("driver_status", driver_status))
    # Add user and other
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(MessageHandler(
        Filters.text(f'{CREATE_USER}'),
        create))
    # Add vehicle to db
    dp.add_handler(MessageHandler(
        Filters.text(f'{CREATE_VEHICLE}'),
        name_vehicle))
    dp.add_handler(MessageHandler(
        Filters.text(f'{USER_DRIVER}') |
        Filters.text(f'{USER_MANAGER_DRIVER}'),
        name))
    # Add vehicle to drivers
    dp.add_handler(CommandHandler("add_vehicle_to_driver", get_list_drivers))
    dp.add_handler(MessageHandler(
        Filters.text(f'{F_UKLON}') |
        Filters.text(f'{F_UBER}') |
        Filters.text(f'{F_BOLT}'),
        get_driver_external_id))

    # The job application on driver sent to fleet
    dp.add_handler(CommandHandler("add_job_application_to_fleets",
                                  get_list_job_application))
    dp.add_handler(MessageHandler(
        Filters.text(f'- {F_BOLT}') |
        Filters.text(f'- {F_UBER}'),
        add_job_application_to_fleet))

    # Commands for Service Station Manager
    # Sending report on repair
    dp.add_handler(CommandHandler("send_report", numberplate_car))
    dp.add_handler(CallbackQueryHandler(inline_buttons))

    # System commands
    dp.add_handler(CommandHandler("cancel", cancel))
    dp.add_handler(MessageHandler(Filters.text, text))
    dp.add_error_handler(error_handler)

    # need fix
    dp.add_handler(CommandHandler('update', update_db, run_async=True))
    dp.add_handler(CommandHandler("save_reports", save_reports))

    dp.add_handler(MessageHandler(Filters.text('Get all today statistic'),
                                  get_manager_today_report))
    dp.add_handler(MessageHandler(Filters.text('Get today statistic'),
                                  get_driver_today_report))
    dp.add_handler(MessageHandler(Filters.text('Choice week number'),
                                  get_driver_week_report))
    dp.add_handler(
        MessageHandler(Filters.text('Update report'), get_update_report))

    updater.job_queue.run_daily(auto_report_for_driver_and_owner,
                                time=datetime.time(7, 0, 0), days=(1,))

    if request.method == 'POST':
        json_string = request.body.decode('utf-8')
        update = Update.de_json(json.loads(json_string), bot)
        dp.process_update(update)
        return HttpResponse(status=200)


def main():

    updater.start_webhook(
        listen='0.0.0.0',
        port=PORT,
        webhook_url=f'{WEBHOOK_URL}/webhook/'
    )
    updater.idle()


def run():
    main()
