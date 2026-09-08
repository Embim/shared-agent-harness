у нас LLM провайдер open ai формата 
сделать agent loop который нужно дорабоать чтобы записывать информацию в базу sqlite 
логиурем tool call skill 
4 tool: bash read(cat) edit create


хоти очень простый харнес который через аутентификацию по jwt токену чтобы в него могли заходить и была параллеьная разработка. одна общая ссеия на нескольких людей + они могли параллеьно ею управлять. 

в рамках localhost   

обернуть в докер py 

3 порта 

2 таблицы пользователи - логов 

морда html 

pivalegies  

id name bash read edit create 
1 admin 1 1 1 1
2 read 0 1 0 0



users 
id name code privaligies start_date end_date 
1 oso ps 1 

logs 
id model timestamp action(tools) user text session inital_promts output_promts status 

                                

input - иницлизировать наше общение, дать хэш - проверить доступность модели, проверить что есть мозности на машине. иницилизировать клиент сессию

отправить провайдеру на анализ analys -> query
вопрос нужен ли тул если да то отправляем список тулов, если нет то отправить сообщение пользователю 

safety-check - проверяем на запрещщные символы -> ок- не ок (обратно(не верный тул))
exucute ()- собираем ответ -> answer analys  

meta_tools 
1 

таблица tools 
id name description text 
